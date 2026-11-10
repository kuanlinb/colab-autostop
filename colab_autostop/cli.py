"""colab-autostop: run unattended jobs on Colab VMs that ship their results and release themselves.

  colab-autostop launch  --session NAME --job "bash train.sh" [--gpu G4] [--upload local=remote ...] ...
  colab-autostop fetch   --session NAME [--dest DIR] [--wait]
  colab-autostop presign put|get KEY [--expires SECONDS]

Storage settings come from AUTOSTOP_* environment variables (see README). Any of them can instead be given as
NAME_FILE pointing to a file that holds the value, so keys can stay in files and out of shell history.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import sys
import tempfile
import time
import urllib.error
import urllib.request

from . import __version__, proc
from .sign import MAX_EXPIRES, PROVIDERS, Storage, storage_from

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "box_guard.sh")
STATE_DIR = os.path.expanduser(os.environ.get("AUTOSTOP_STATE_DIR", "~/.colab-autostop/runs"))
COLAB_IGNORE = "/content/.config /content/drive /content/sample_data"
ARMED_MARK = "AUTOSTOP ARMED"

# Runs on the VM through `colab exec`. Moves the staged files into place, starts the guard detached and waits
# until it reports armed, and only then starts the job, also detached. The exec itself returns in seconds.
BOX_STARTER = r'''
import os, shutil, subprocess, time
D, STAGE, NAMES, CWD = {box_dir!r}, {stage!r}, {names!r}, {cwd!r}
os.makedirs(D, exist_ok=True)
for n in NAMES:
    shutil.move(STAGE + n, os.path.join(D, n))
def spawn(args, out):
    return subprocess.Popen(args, cwd=CWD, stdin=subprocess.DEVNULL, stdout=open(out, "ab"),
                            stderr=subprocess.STDOUT, start_new_session=True)
guard = spawn(["bash", D + "/box_guard.sh", D + "/autostop.env"], D + "/guard.out")
log, armed = D + "/guard.log", False
for _ in range(60):
    time.sleep(0.5)
    text = open(log).read() if os.path.exists(log) else ""
    if "armed:" in text and "not armed" not in text:
        armed = True
        break
    if guard.poll() is not None:
        break
if armed:
    job = spawn(["bash", D + "/run_job.sh"], D + "/run_job.out")
    with open(D + "/job.pid", "w") as f:  # the job leads its own session; the guard sums that session's CPU
        f.write(str(job.pid))
    print("{armed_mark} guard pid", guard.pid, "job pid", job.pid)
else:
    print("AUTOSTOP NOT ARMED; job not started. guard.log:")
    print(open(log).read() if os.path.exists(log) else "(no guard.log)")
'''

RUN_JOB = """#!/usr/bin/env bash
# written by colab-autostop launch: run the job, then leave its exit code as the done marker
cd {cwd}
export PYTHONUNBUFFERED=1  # Python output reaches the log (and counts as progress) as it is printed
bash {d}/job_cmd.sh > {log} 2>&1
echo $? > {d}/job.exit.tmp && mv -f {d}/job.exit.tmp {d}/job.exit
"""


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def split_cmd(s):
    """Split a command string; on Windows keep backslashes and drop surrounding quotes."""
    if os.name == "nt":
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(s, posix=False)]
    return shlex.split(s)


class Colab:
    """The official colab CLI, every call with a hard time limit."""

    def __init__(self, cmd, timeout):
        self.base = split_cmd(cmd)
        self.timeout = timeout

    def __call__(self, *args, timeout=None):
        r = proc.run(self.base + list(args), timeout or self.timeout)
        if r.timed_out:
            log(f"colab {args[0]} hit its {timeout or self.timeout:g} s limit and was killed")
        return r

    def listed(self, session):
        """True / False, or None when the session list could not be read."""
        r = self("sessions")
        if f"[{session}]" in r.out:
            return True
        if r.ok:
            return False
        return None


def storage_args(ap):
    g = ap.add_argument_group("storage (defaults from AUTOSTOP_* environment variables)")
    g.add_argument("--provider", choices=PROVIDERS, help="default: $AUTOSTOP_PROVIDER or r2")
    g.add_argument("--bucket")
    g.add_argument("--account-id", help="R2 account id")
    g.add_argument("--region", help="S3 region (default us-east-1)")
    g.add_argument("--endpoint", help="custom S3-compatible endpoint, e.g. https://minio.local:9000")
    g.add_argument("--path-style", action="store_true", help="S3: put the bucket in the path")


def storage_of(a, record=None):
    rec = record or {}
    return storage_from(provider=a.provider or rec.get("provider"), bucket=a.bucket or rec.get("bucket"),
                        account_id=a.account_id or rec.get("account_id"), region=a.region or rec.get("region"),
                        endpoint=a.endpoint or rec.get("endpoint"),
                        path_style=a.path_style or rec.get("path_style"))


# ---------------------------------------------------------------- launch

def build_box_files(a, key_prefix, storage, tmp):
    """Write the files the VM needs into tmp; return {name: local path}."""
    d = a.box_dir.rstrip("/")
    job_log = a.job_log
    done = [f"{d}/job.exit"] if not a.no_exit_done else []
    done += a.done_file
    results = a.results + [job_log]
    expires = min(MAX_EXPIRES, int(a.cap_min * 60 + a.url_grace_hours * 3600))
    env = {
        "AUTOSTOP_DIR": d,
        "AUTOSTOP_WATCH": " ".join(a.watch),
        "AUTOSTOP_IGNORE": " ".join([COLAB_IGNORE] + a.ignore),  # Colab's own writes never count
        "AUTOSTOP_IDLE_SEC": str(int(a.idle_min * 60)),
        "AUTOSTOP_CAP_SEC": str(int(a.cap_min * 60)),
        "AUTOSTOP_POLL_SEC": str(a.poll_sec),
        "AUTOSTOP_GPU_BUSY_PCT": str(a.gpu_busy_pct),
        "AUTOSTOP_CPU_BUSY_PCT": str(a.cpu_busy_pct),
        "AUTOSTOP_DONE_FILE": " ".join(done),
        "AUTOSTOP_DONE_LOG": a.done_log or (job_log if a.done_regex else ""),
        "AUTOSTOP_DONE_REGEX": a.done_regex or "",
        "AUTOSTOP_RESULTS": " ".join(results),
        "AUTOSTOP_FALLBACK_WAIT_MIN": str(a.fallback_wait_min),
    }
    files = {}

    def put(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        files[name] = path

    put("autostop.env", "".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
    put("job_cmd.sh", a.job.rstrip("\n") + "\n")
    put("run_job.sh", RUN_JOB.format(cwd=shlex.quote(a.cwd), d=shlex.quote(d), log=shlex.quote(job_log)))
    put("put_results.url", storage.presign("PUT", f"{key_prefix}/results.tgz", expires))
    put("put_status.url", storage.presign("PUT", f"{key_prefix}/status.txt", expires))
    files["box_guard.sh"] = GUARD
    return files


def check_vm_paths(a):
    """Paths on the VM must be absolute POSIX paths. Git Bash rewrites /content/x into C:/Program Files/Git/...
    when it starts a Windows program, which would silently pack and watch the wrong places."""
    paths = [("--cwd", a.cwd), ("--job-log", a.job_log), ("--box-dir", a.box_dir)]
    paths += [(n, p) for n, ps in (("--results", a.results), ("--watch", a.watch), ("--ignore", a.ignore),
                                   ("--done-file", a.done_file)) for p in ps]
    paths += [("--done-log", a.done_log)] if a.done_log else []
    paths += [("--upload", s.split("=", 1)[1]) for s in a.upload if "=" in s]
    for name, p in paths:
        if not p.startswith("/") or "\\" in p or ":" in p or " " in p:
            hint = (" (Git Bash rewrites /paths for Windows programs; run with MSYS_NO_PATHCONV=1)"
                    if ":" in p or "\\" in p else "")
            sys.exit(f"{name} {p!r}: paths on the VM must be absolute POSIX paths without spaces{hint}")


def cmd_launch(a):
    if not os.path.exists(GUARD):
        sys.exit(f"box_guard.sh missing next to {__file__}")
    check_vm_paths(a)
    if a.cpu_busy_pct != "off" and not a.cpu_busy_pct.isdigit():
        sys.exit("--cpu-busy-pct must be a whole number or off")
    if a.cap_min * 60 + 3600 > MAX_EXPIRES:
        sys.exit("--cap-min is too long: the upload URLs (max 7 days) would expire before the cap")
    for spec in a.upload:
        if "=" not in spec or not os.path.isfile(spec.split("=", 1)[0]):
            sys.exit(f"--upload needs LOCAL=REMOTE with an existing local file: {spec}")
    try:
        storage = storage_of(a)  # fail on missing keys before renting anything
    except ValueError as e:
        sys.exit(str(e))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key_prefix = f"{a.key_prefix.strip('/')}/{a.session}/{stamp}"
    colab = Colab(a.colab, a.cli_timeout)

    # An existing session with this name would be reused by mistake: its old guard and done marker would end
    # the new run at once. Refuse instead.
    before = colab.listed(a.session)
    if before is not False:
        sys.exit(f"a session named {a.session} already exists; pick another name or stop it first"
                 if before else "could not read `colab sessions`; check that the colab CLI works and is logged in")

    def abort(why):
        log(f"launch failed: {why}")
        if a.keep_on_error:
            sys.exit(f"launch failed: {why}\nleft {a.session} running (--keep-on-error); "
                     f"stop it with `colab stop -s {a.session}`")
        colab("stop", "-s", a.session, timeout=a.cli_timeout)
        gone = colab.listed(a.session)
        sys.exit(f"launch failed: {why}\nstopped {a.session}" + ("" if gone is False else
                 f"; could not confirm it is gone, check `colab sessions` (got {gone})"))

    try:
        start_on_vm(a, colab, storage, key_prefix, abort)
    except SystemExit:
        raise
    except BaseException as e:  # Ctrl-C or an unexpected error after renting: never leave the VM billing
        abort(f"interrupted ({type(e).__name__}: {e})")

    record = {"session": a.session, "key_prefix": key_prefix, "started": stamp, "gpu": a.gpu,
              "idle_min": a.idle_min, "cap_min": a.cap_min, "storage": storage.public_config()}
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{a.session}.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    log(f"job running; results will land at {key_prefix}/ ; fetch with: colab-autostop fetch --session {a.session}")


def start_on_vm(a, colab, storage, key_prefix, abort):
    """colab new, upload the guard and the job, start both. Calls abort() on any failure after the VM exists."""
    log(f"creating session {a.session} ({a.gpu or 'CPU'})")
    new_args = ["new", "-s", a.session] + (["--gpu", a.gpu] if a.gpu else [])
    r = colab(*new_args, timeout=a.new_timeout)
    present = colab.listed(a.session)
    if present is not True and not r.timed_out and not r.ok:
        sys.exit(f"colab new failed:\n{r.out.strip()}")
    if present is not True:
        # A timed-out `new` may still be assigning; look once more so a late VM is not left billing unseen.
        time.sleep(30)
        present = colab.listed(a.session)
        if present is not True:
            sys.exit(f"session {a.session} did not appear (sessions check: {present}). "
                     f"Run `colab sessions` later to be sure nothing is billing.\n{r.out.strip()}")
    log("session is up")

    stage = "/content/autostop-staging-"
    with tempfile.TemporaryDirectory() as tmp:
        files = build_box_files(a, key_prefix, storage, tmp)
        for name, local in files.items():
            r = colab("upload", "-s", a.session, local, stage + name, timeout=a.upload_timeout)
            if not r.ok:
                abort(f"upload of {name} failed:\n{r.out.strip()}")
        for spec in a.upload:
            local, remote = spec.split("=", 1)
            log(f"uploading {local} -> {remote}")
            r = colab("upload", "-s", a.session, local, remote, timeout=a.upload_timeout)
            if not r.ok:
                abort(f"upload of {local} failed:\n{r.out.strip()}")
        starter = os.path.join(tmp, "start_on_box.py")
        with open(starter, "w", encoding="utf-8", newline="\n") as f:
            f.write(BOX_STARTER.format(box_dir=a.box_dir.rstrip("/"), stage=stage, names=sorted(files),
                                       cwd=a.cwd, armed_mark=ARMED_MARK))
        r = colab("exec", "-s", a.session, "-f", starter, "--timeout", "60", timeout=a.cli_timeout)
    print(r.out.strip())
    if ARMED_MARK not in r.out:
        abort("the guard did not report armed, so the job was not started")


# ---------------------------------------------------------------- fetch

def download(storage, key, dest):
    """True if downloaded, False if the object does not exist."""
    try:
        with urllib.request.urlopen(storage.presign("GET", key, 900), timeout=300) as r:
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        os.replace(tmp, dest)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        if e.code == 403 and storage.provider == "s3":
            # S3 answers 403 instead of 404 for a missing key when the key pair may not list the bucket
            log("S3 returned 403; treating it as 'not there yet' (it can also mean wrong keys or bucket)")
            return False
        raise


def cmd_fetch(a):
    record = {}
    if a.session:
        path = os.path.join(STATE_DIR, f"{a.session}.json")
        if not os.path.exists(path):
            sys.exit(f"no launch record {path}; pass --key instead")
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    prefix = (a.key or record.get("key_prefix", "")).strip("/")
    if not prefix:
        sys.exit("give --session or --key")
    try:
        storage = storage_of(a, record.get("storage"))
    except ValueError as e:
        sys.exit(str(e))
    name = a.session or prefix.replace("/", "_")
    os.makedirs(a.dest, exist_ok=True)
    status_path = os.path.join(a.dest, f"{name}.status.txt")
    deadline = time.time() + a.max_wait_min * 60
    while not download(storage, f"{prefix}/status.txt", status_path):
        if not a.wait or time.time() > deadline:
            print("not finished yet (no status.txt)")
            return 2
        time.sleep(a.poll_sec)
    with open(status_path, encoding="utf-8") as f:
        status = f.read().strip()
    results = os.path.join(a.dest, f"{name}.results.tgz")
    got = download(storage, f"{prefix}/results.tgz", results)
    print(f"status: {status}")
    print(f"results: {results if got else 'missing'}")
    if not got:
        return 1
    if not status.startswith("DONE"):
        return 3
    m = re.search(r"exit code (-?\d+)", status)
    return 4 if m and m.group(1) != "0" else 0


# ---------------------------------------------------------------- presign

def cmd_presign(a):
    try:
        storage = storage_of(a)
        print(storage.presign(a.method.upper(), a.key, a.expires))
    except ValueError as e:
        sys.exit(str(e))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="colab-autostop", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("launch", help="rent a VM, arm the guard, start the job")
    p.add_argument("--session", required=True, help="Colab session name")
    p.add_argument("--job", required=True, help="shell command run on the VM (in --cwd)")
    p.add_argument("--gpu", help="T4, L4, G4, A100, H100; omit for a CPU runtime")
    p.add_argument("--upload", action="append", default=[], metavar="LOCAL=REMOTE",
                   help="extra file to upload before the job starts (remote parent dir must exist)")
    p.add_argument("--results", action="append", default=[], metavar="PATH",
                   help="path on the VM packed into results.tgz (repeatable; the job log is always added)")
    p.add_argument("--idle-min", type=float, default=20, help="minutes without progress before STUCK (20)")
    p.add_argument("--cap-min", type=float, default=720, help="hard cap in minutes (720)")
    p.add_argument("--watch", action="append", default=None, metavar="PATH",
                   help="paths whose writes count as progress (default /content)")
    p.add_argument("--ignore", action="append", default=[], metavar="PATH",
                   help="more paths under --watch that never count (Colab's own housekeeping dirs always)")
    p.add_argument("--gpu-busy-pct", type=int, default=5, help="GPU use at or above this counts as progress (5)")
    p.add_argument("--cpu-busy-pct", default="30",
                   help="CPU use of the job's own processes at or above this, in %% of one core, "
                        "counts as progress (30; off)")
    p.add_argument("--done-file", action="append", default=[], metavar="PATH",
                   help="extra done marker file on the VM (the job's own exit is always one)")
    p.add_argument("--done-regex", help="DONE when this regex matches a line of --done-log (default: the job log)")
    p.add_argument("--done-log", help="log file searched by --done-regex")
    p.add_argument("--no-exit-done", action="store_true",
                   help="do not treat the job command exiting as DONE (for jobs that start a daemon and return)")
    p.add_argument("--poll-sec", type=int, default=30, help="guard check interval on the VM (30)")
    p.add_argument("--fallback-wait-min", type=float, default=45,
                   help="if the upload fails, keep the VM this long so you can `colab download` (45)")
    p.add_argument("--cwd", default="/content", help="working directory of the job on the VM")
    p.add_argument("--job-log", default="/content/autostop-job.log")
    p.add_argument("--box-dir", default="/content/autostop", help="guard state dir on the VM (never watched)")
    p.add_argument("--key-prefix", default="colab-autostop", help="object key prefix in the bucket")
    p.add_argument("--url-grace-hours", type=float, default=24, help="upload URLs live cap + this (max 7 days)")
    p.add_argument("--colab", default=os.environ.get("AUTOSTOP_COLAB", "colab"),
                   help="command that runs the colab CLI (default: $AUTOSTOP_COLAB or colab)")
    p.add_argument("--new-timeout", type=float, default=300, help="time limit for `colab new` in seconds")
    p.add_argument("--upload-timeout", type=float, default=600, help="time limit per `colab upload`")
    p.add_argument("--cli-timeout", type=float, default=150, help="time limit for other colab calls")
    p.add_argument("--keep-on-error", action="store_true", help="do not stop the VM when launching fails")
    storage_args(p)
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("fetch", help="download status.txt and results.tgz from the bucket")
    p.add_argument("--session", help="session name used at launch (reads the local launch record)")
    p.add_argument("--key", help="key prefix printed at launch, instead of --session")
    p.add_argument("--dest", default=".", help="local folder (default .)")
    p.add_argument("--wait", action="store_true", help="poll until the run has finished")
    p.add_argument("--poll-sec", type=float, default=120)
    p.add_argument("--max-wait-min", type=float, default=24 * 60)
    storage_args(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("presign", help="print a presigned URL (for using box_guard.sh on its own)")
    p.add_argument("method", choices=["put", "get"])
    p.add_argument("key")
    p.add_argument("--expires", type=int, default=3600, help="seconds, at most 604800 (7 days)")
    storage_args(p)
    p.set_defaults(func=cmd_presign)

    a = ap.parse_args(argv)
    if a.cmd == "launch" and not a.watch:
        a.watch = ["/content"]
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
