# colab-autostop

Run unattended jobs on Google Colab VMs that **upload their own results and release their own VM** when the job
finishes, gets stuck, or hits a hard time cap. The computer that started the job can be asleep, rebooting or
offline the whole time.

[繁體中文說明](README.zh-TW.md)

## The problem

A Colab runtime keeps billing compute units for as long as it is assigned. The official `colab` CLI and the
notebook UI both keep the session alive while a kernel is active, which is what you want while a job runs, and
exactly what you do not want after it ends or hangs: nothing ends the VM until someone stops it.

The usual fix is a watcher on your laptop that polls the VM and runs `colab stop` at the end. That watcher fails
precisely when it matters: the laptop sleeps, reboots, loses Wi-Fi, or the watcher itself hangs. In the incident
that started this project, a laptop watcher's `colab exec` call hung; a G4 GPU VM whose job had already finished
sat idle for 3 h 40 min and burned about 37 compute units.

`colab-autostop` moves the decision onto the VM. A small bash guard runs next to the job, and when the run is over
it ships the results to object storage with presigned URLs and releases the VM itself.

## How it works

```
laptop: colab-autostop launch                         Colab VM
  1. colab new ......................................> VM assigned
  2. sign two PUT URLs (results.tgz, status.txt)
  3. colab upload: guard, settings, URLs, job .......> /content/autostop/
  4. colab exec a 1-second starter ..................> starts box_guard.sh (detached), waits for "armed",
     (returns right away; every CLI call                then starts your job (detached)
      has a hard time limit and is killed with
      its whole process tree when it overruns)        guard, every 30 s:
                                                        DONE?  STUCK?  CAP?
  ...laptop can now be off...                           on any of them:
                                                          tar results -> curl PUT results.tgz, status.txt
                                                          POST http://$TBE_RUNTIME_ADDR/unassign  (VM released)
any machine with the bucket keys, any time later:
  colab-autostop fetch  <................................ bucket
```

If launching fails after the VM was created (an upload fails, the guard does not arm, `exec` hangs), the launcher
stops the VM before exiting, so a failed launch never leaves a machine billing.

### Three end states

| State | When | What is shipped |
|---|---|---|
| `DONE` | a done marker appears: by default the job command exits (its exit code is reported), or any `--done-file` exists, or `--done-regex` matches a line of the log | result paths, job log, guard log |
| `STUCK` | for `--idle-min` minutes (default 20) there was no progress: no file under the watched paths was written, GPU utilization stayed below 5 %, and the job's own processes used less than 30 % of one CPU core | whatever exists so far, same archive |
| `CAP` | `--cap-min` minutes (default 720) since the guard started, whatever the job is doing | whatever exists so far |

`status.txt` holds one line such as `DONE 2026-11-10T08:12:40Z done marker found, exit code 0`. It is uploaded
after `results.tgz`, so when the status exists the archive is complete. The archive keeps the result paths as they
are on the VM (`content/out/...`) and adds the guard's own log as `guard.log`, with a reading of the progress
signals every 10 polls. The guard folder itself (upload URLs) is never packed.

The job's output goes to `/content/autostop-job.log`, which is watched, so a job that prints counts as alive.
The launcher sets `PYTHONUNBUFFERED=1` so Python output reaches that log as it is printed.

If the upload fails 5 times, the guard keeps the VM for `--fallback-wait-min` minutes (default 45) so you can still
`colab download /content/autostop/results.tgz`, then releases it anyway. If no upload URLs are configured at all,
it releases right away. If the release request fails, the guard tries Colab's own `runtime.unassign()` once and
keeps retrying the request every minute for 30 minutes before giving up. Every probe (`nvidia-smi`, `find`, `tar`)
runs under a time limit, so a wedged GPU or a hung network mount cannot freeze the loop.

## Install

Python 3.8+, no dependencies. You also need the official [Colab CLI](https://pypi.org/project/google-colab-cli/),
logged in.

```bash
pip install git+https://github.com/kuanlinb/colab-autostop.git
```

Or from a clone, to run the tests too:

```bash
git clone https://github.com/kuanlinb/colab-autostop.git && cd colab-autostop
pip install .
```

## Quick start

1. Make a private bucket and an access key that can only read and write that bucket (on R2: an API token with
   "Object Read & Write" on one bucket).
2. Tell the tool where it is. Each setting can be given directly, or as `NAME_FILE` pointing to a file that holds
   it, which keeps secrets out of shell history:

   ```bash
   export AUTOSTOP_PROVIDER=r2                 # r2 | s3 | gcs | custom
   export AUTOSTOP_BUCKET=colab-results
   export AUTOSTOP_ACCOUNT_ID=<cloudflare account id>          # R2 only
   export AUTOSTOP_ACCESS_KEY_ID_FILE=~/.secrets/r2_key_id
   export AUTOSTOP_SECRET_ACCESS_KEY_FILE=~/.secrets/r2_secret
   ```

3. Launch:

   ```bash
   colab-autostop launch --session train1 --gpu G4 \
     --upload ./train.tgz=/content/train.tgz \
     --job "tar xzf train.tgz && python train.py --out /content/out" \
     --results /content/out --idle-min 20 --cap-min 360
   ```

4. Later, from anywhere:

   ```bash
   colab-autostop fetch --session train1 --dest ./pulled          # once
   colab-autostop fetch --session train1 --dest ./pulled --wait   # poll until the run has ended
   ```

   Exit code: 0 for `DONE` with exit code 0, 4 for `DONE` with a non-zero exit code, 3 for `STUCK` or `CAP`,
   2 while the run has not ended, 1 on errors.
   `fetch --key <prefix>` works on a machine that has no launch record.

If your `colab` CLI is behind a wrapper (another account, another profile), pass it with `--colab` or
`AUTOSTOP_COLAB`, for example `--colab "powershell -NoProfile -File C:\path\colab_b.ps1"`.

On Windows, Git Bash rewrites arguments that look like `/content/out` into `C:/Program Files/Git/content/out`
before Python sees them. Run the launcher from PowerShell or cmd, or set `MSYS_NO_PATHCONV=1` in Git Bash. The
launcher refuses VM paths that are not absolute POSIX paths, so this fails loudly instead of packing nothing.

`status.txt` is uploaded just before the release request, so it cannot say whether the release worked. To be sure
nothing is still billing, check `colab sessions` (the next `colab` command also prunes sessions that Colab has
already ended).

### Storage providers

| `AUTOSTOP_PROVIDER` | Also needs | URL form | Status |
|---|---|---|---|
| `r2` (default) | `AUTOSTOP_ACCOUNT_ID` | `https://<account>.r2.cloudflarestorage.com/<bucket>/<key>` | tested on live Colab runs |
| `s3` | `AUTOSTOP_REGION` (default `us-east-1`); `AUTOSTOP_PATH_STYLE=1` for path-style | `https://<bucket>.s3.<region>.amazonaws.com/<key>` | experimental |
| `gcs` | an [HMAC key](https://cloud.google.com/storage/docs/authentication/hmackeys) | `https://storage.googleapis.com/<bucket>/<key>`, `GOOG4-HMAC-SHA256` | experimental |
| `custom` | `AUTOSTOP_ENDPOINT`, e.g. `https://minio.example.com:9000` | path-style, SigV4 | experimental |

*Experimental* means the signing passes the published AWS test vector and the URL format follows the provider's
documentation, but no upload has been tried against a real bucket of that kind yet. If you use one, please report
whether it worked.

`colab-autostop presign put|get KEY` prints a single presigned URL with the same settings.

### Useful launch options

| Option | Default | Meaning |
|---|---|---|
| `--watch PATH` | `/content` | writes here count as progress (repeatable) |
| `--ignore PATH` | none | more paths that never count as progress; Colab's own `/content/.config`, `/content/drive` and `/content/sample_data` are always ignored |
| `--gpu-busy-pct N` | 5 | GPU use at or above N % counts as progress; 101 turns the GPU signal off |
| `--cpu-busy-pct N` | 30 | CPU use of the job's own processes at or above N % of one core, averaged over a poll, counts as progress; `off` turns it off |
| `--done-file PATH` | none | extra done marker on the VM |
| `--done-regex RE` | none | DONE when RE matches a line of `--done-log` (default: the job log) |
| `--no-exit-done` | off | for jobs that start a daemon and return at once |
| `--poll-sec N` | 30 | guard check interval |
| `--new-timeout`, `--upload-timeout`, `--cli-timeout` | 300, 600, 150 s | hard limits for each CLI call |
| `--keep-on-error` | off | leave the VM up when launching fails, for debugging |

## Using the guard on its own

`colab_autostop/box_guard.sh` is self-contained bash and can be used from a notebook without the launcher. Copy it
to the VM, sign two PUT URLs on your own machine (`colab-autostop presign put <prefix>/results.tgz --expires 86400`),
and start it detached:

```python
import os, subprocess
os.makedirs("/content/autostop", exist_ok=True)
open("/content/autostop/put_results.url", "w").write("<PUT URL for results.tgz>")
open("/content/autostop/put_status.url", "w").write("<PUT URL for status.txt>")
env = dict(os.environ, AUTOSTOP_RESULTS="/content/out", AUTOSTOP_DONE_FILE="/content/out/DONE",
           AUTOSTOP_IDLE_MIN="20", AUTOSTOP_CAP_MIN="360")
subprocess.Popen(["bash", "/content/box_guard.sh"], env=env, start_new_session=True)
```

Your code then writes `/content/out/DONE` when it finishes. To use CPU as a progress signal, write the pid of a
process that leads your job's session to `/content/autostop/job.pid` (or point `AUTOSTOP_JOB_PID_FILE` at it);
without it only file writes and the GPU count. All settings are listed at the top of the script.

## Security design

- **No long-lived credential ever reaches the VM.** The VM gets two presigned PUT URLs, each valid for exactly one
  object key, only for PUT, and only until the cap plus 24 hours (never more than 7 days, the SigV4 limit). If one
  leaks, the worst case is that someone overwrites those two objects until the URL expires. It cannot read, list or
  delete anything else in the bucket.
- Downloads use GET URLs signed on your machine that expire after 15 minutes.
- Keys are read from the environment or from files. The tool never writes them anywhere; the local launch record
  (`~/.colab-autostop/runs/<session>.json`) holds only the provider, bucket name and key prefix.
- Use a dedicated bucket and a key scoped to that bucket.
- The URLs live in `/content/autostop` on the VM, which is destroyed when the VM is released.

## Limitations

- **Release uses an undocumented endpoint.** The guard POSTs to `http://$TBE_RUNTIME_ADDR/unassign`, the same
  request `google.colab.runtime.unassign()` sends. It is an internal Colab interface with no public documentation
  and may change or disappear. If it fails, the guard logs it, tries `runtime.unassign()` once, then retries every minute for 30 minutes before it exits; the results are still
  uploaded, but the VM keeps billing until Colab reclaims it or you stop it. Keep a backstop of your own (for
  example a scheduled `colab stop` at cap plus a grace period) for anything expensive.
- **Silence looks like a hang.** Progress means "some file under the watched paths was written, or the GPU or the
  CPU is busy". A job that only waits (for a remote API, for a slow download into an unwatched path) and writes
  nothing for longer than `--idle-min` is judged `STUCK`. Raise `--idle-min`, watch more paths, or have the job
  touch a heartbeat file.
- **CPU counts only for the job's own processes**, meaning every process in the session the launcher starts the
  job in. A first version used whole-VM CPU and a dead job never became `STUCK`: an idle Colab CPU VM's own
  services used 17 to 29 % of a core, with a spike above 30 % every couple of minutes. A daemon your job starts in
  a new session (`setsid`, `start_new_session=True`) is not counted.
- **Busy is not the same as healthy.** A process spinning the GPU or a CPU core in a deadlock, or printing the same
  error forever, is not `STUCK`; only `CAP` ends it. Always set a cap you can afford.
- Results go up in one PUT request. S3 and R2 limit a single PUT to 5 GiB; keep large checkpoints elsewhere or
  split them.
- `colab upload` sends a whole file in one request; for large inputs, download them on the VM instead.
- Paths in lists are space separated, so paths containing spaces are not supported.
- Signatures carry a timestamp, so the clock of the machine that signs must be roughly right.
- `tar` writes the archive to the VM's disk, so it needs free space about the size of the compressed results.
- A custom endpoint written with an explicit default port (`https://host:443`) may be signed with a host header
  that differs from what curl sends; leave the default port out.
- The guard needs bash, `find`, `tar` and `curl`, which Colab images have.

## Compared with other approaches

| | colab-autostop | `runtime.unassign()` at the end of a notebook, results to Drive | SkyPilot autostop | `colab run` |
|---|---|---|---|---|
| Ends the VM after the job | yes | yes, if the last cell runs | yes | yes |
| Ends a hung or stuck job | yes, progress-based (`STUCK`) and a hard cap | no: the last cell never runs | hard limit only (`wait_for: none`); a hung running job counts as busy | no |
| Works with your computer off | yes | yes | yes | no: the local CLI process drives it |
| Gets results off the VM | object storage, presigned URLs, no credential on the VM | Drive mount, needs an interactive Google sign-in in the session | its own storage mounts | you copy them yourself |
| Runs on Colab | yes | yes | no, Colab is not one of its clouds | yes |

SkyPilot is the right tool on clouds it supports; this project exists because Colab is not one of them and its
runtimes have no autostop of their own.

## Tests

```bash
python -m unittest -v
```

Nothing needs Colab or network access:

- `tests/test_sign.py`: signatures, including the worked example from the AWS SigV4 query-string documentation,
  and URL formats for R2, S3, GCS and custom endpoints.
- `tests/test_guard.py`: the guard's state machine with a fake `curl`, a fake `nvidia-smi` and a fake
  `/proc/stat` (DONE by exit code or regex, STUCK, CAP, its own writes and ignored paths not counting as progress,
  GPU-busy, CPU of the job's session measured per poll while other sessions' CPU is ignored (also against the real
  `/proc` with a spinning and a sleeping process), a hung `nvidia-smi`, upload failure, release failure, URLs never packed,
  not on Colab), plus the whole VM-side start-up run in a temp folder. Needs bash, so it runs on Linux
  or WSL and is skipped elsewhere (native Windows, macOS).
- `tests/test_launch.py`: the launcher against a fake `colab` CLI wrapped like a shell wrapper would be: hung `exec`
  and hung `new` are killed with their whole process tree and the VM is stopped; failed uploads, an unarmed guard
  and Ctrl-C stop the VM; an existing session name is refused; no secret is ever uploaded.

Live checks so far (2026-11-10, Colab CPU runtimes and one G4 GPU runtime, Cloudflare R2, `colab` CLI 0.7.5; every test VM released
itself and `colab sessions` was empty afterwards; launching took about 30 s):

| Run | Job | Result |
|---|---|---|
| DONE | sleeps 75 s, writes `out/acc.txt`, exits 0 | `DONE` 1.5 min after start, exit code 0, result file in the archive |
| STUCK | writes one file, then sleeps | `STUCK` exactly 3 min after the last write (`--idle-min 3`); the job's CPU read 0 % |
| CAP | writes a line every 20 s | `CAP` at the 4 min cap |
| CPU only | `while True: pass`, writes nothing | job CPU read 98 to 99 % of a core, never `STUCK`, `CAP` at the 6 min cap |
| G4 GPU | 5 min of matrix multiplies writing nothing, then holds GPU memory and sleeps (`--cpu-busy-pct off`, so only the GPU reading can keep it alive) | GPU read 100 % then 0 %; survived the 5 silent minutes and was `STUCK` exactly 3 min after the GPU went idle (8 min after start); about 1.2 compute units |

The STUCK run failed with the first version, which used whole-VM CPU: the idle VM's own services kept resetting the
idle timer and the dead job ran to the cap. That is why CPU is now counted for the job's processes only.

Not tested live yet: AWS S3, GCS, and GPU types other than G4. S3 and GCS signing is checked only against the published test
vector and format rules.

## License

MIT. Not affiliated with Google or Colab.
