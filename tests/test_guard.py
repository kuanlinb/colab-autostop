"""State machine tests for box_guard.sh with a fake curl and a fake nvidia-smi. No Colab needed.

The guard only runs on Colab's Linux VMs, so these tests need a POSIX system with bash, GNU-style find and tar:
Linux, WSL, or macOS. On native Windows they are skipped; run them under WSL.
"""
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, os.pardir, "colab_autostop", "box_guard.sh")
FAKES = os.path.join(HERE, "fakes")
BASH = None if os.name == "nt" else shutil.which("bash")


def sh_path(p):
    return os.path.abspath(p)


@unittest.skipIf(BASH is None, "needs bash on Linux/macOS/WSL (the guard runs on Linux)")
class GuardStates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autostop-test-")
        self.root = os.path.join(self.tmp, "content")       # stands in for /content
        self.box = os.path.join(self.root, "autostop")      # guard dir, inside the watched tree on purpose
        self.store = os.path.join(self.tmp, "store")        # what the fake curl "uploaded"
        self.out = os.path.join(self.root, "out")
        for d in (self.root, self.box, self.store, self.out, os.path.join(self.root, ".config")):
            os.makedirs(d)
        with open(os.path.join(self.out, "metrics.json"), "w") as f:
            f.write('{"acc": 0.9}\n')
        for name in ("put_results.url", "put_status.url"):
            with open(os.path.join(self.box, name), "w") as f:
                f.write(f"https://bucket.example/jobs/x/{name.replace('put_', '').replace('.url', '')}"
                        f"{'.tgz' if 'results' in name else '.txt'}?X-Amz-Signature=abc\n")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.fake_path(),
            "FAKE_STORE": sh_path(self.store),
            "TBE_RUNTIME_ADDR": "172.28.0.1:8080",
            "AUTOSTOP_DIR": sh_path(self.box),
            "AUTOSTOP_WATCH": sh_path(self.root),
            "AUTOSTOP_IGNORE": sh_path(os.path.join(self.root, ".config")),
            "AUTOSTOP_POLL_SEC": "1",
            "AUTOSTOP_IDLE_SEC": "3",
            "AUTOSTOP_CAP_SEC": "60",
            "AUTOSTOP_DONE_FILE": sh_path(os.path.join(self.box, "job.exit")),
            "AUTOSTOP_RESULTS": sh_path(self.out),
            "AUTOSTOP_UPLOAD_RETRIES": "2",
            "AUTOSTOP_RETRY_SLEEP_SEC": "0",
            "AUTOSTOP_FALLBACK_WAIT_SEC": "2",
            "AUTOSTOP_RELEASE_RETRY_SEC": "0",
            "AUTOSTOP_RELEASE_TRIES": "3",
            "AUTOSTOP_CPU_BUSY_PCT": "off",  # CPU tests opt in, with a fake /proc
            "AUTOSTOP_PROC_DIR": os.path.join(self.tmp, "proc"),
            "AUTOSTOP_JOB_PID_FILE": os.path.join(self.tmp, "job.pid"),
            "AUTOSTOP_CLK_TCK": "100",
            "FAKE_GPU": "0",
        })
        self.stop_writer = threading.Event()

    def fake_path(self):
        return FAKES + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        self.stop_writer.set()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -------------------------------------------------------------
    def run_guard(self, timeout=60, **env):
        e = dict(self.env)
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        t0 = time.time()
        p = subprocess.run([BASH, sh_path(GUARD)], env=e, capture_output=True, text=True, timeout=timeout)
        self.elapsed = time.time() - t0
        self.code = p.returncode
        return p

    def later(self, delay, fn):
        threading.Timer(delay, fn).start()

    def keep_writing(self, path, every=0.4):
        def loop():
            i = 0
            while not self.stop_writer.is_set():
                with open(path, "a") as f:
                    f.write(f"step {i}\n")
                i += 1
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()

    @staticmethod
    def write(path, text, mode="w"):
        with open(path, mode) as f:
            f.write(text)

    def read(self, *parts):
        p = os.path.join(*parts)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()

    def status(self):
        return self.read(self.store, "status.txt")

    def released(self):
        r = self.read(self.store, "released")
        return r.splitlines() if r else []

    def archive_names(self):
        with open(os.path.join(self.store, "results.tgz"), "rb") as f:
            with tarfile.open(fileobj=io.BytesIO(f.read())) as t:
                return t.getnames()

    def guard_log(self):
        return self.read(self.box, "guard.log") or ""

    # states --------------------------------------------------------------
    def test_done_marker_ships_results_then_status_then_releases(self):
        self.keep_writing(os.path.join(self.root, "train.log"))  # job alive until it finishes
        def finish():
            self.stop_writer.set()
            with open(os.path.join(self.box, "job.exit"), "w") as f:
                f.write("0\n")
        self.later(2.5, finish)
        self.run_guard()
        self.assertEqual(self.code, 0, self.guard_log())
        self.assertRegex(self.status(), r"^DONE \S+Z done marker found, exit code 0")
        self.assertTrue(any(n.endswith("out/metrics.json") for n in self.archive_names()), self.archive_names())
        self.assertTrue(any(n == "guard.log" for n in self.archive_names()))
        self.assertEqual(self.released(), ["http://172.28.0.1:8080/unassign"])
        targets = [c.split()[-1].split("?")[0].rsplit("/", 1)[-1]
                   for c in self.read(self.store, "curl.calls").splitlines()]
        self.assertEqual(targets, ["results.tgz", "status.txt", "unassign"])

    def test_failed_job_exit_code_is_reported(self):
        self.later(1.5, lambda: self.write(os.path.join(self.box, "job.exit"), "137\n"))
        self.run_guard()
        self.assertRegex(self.status(), r"^DONE .*exit code 137")

    def test_done_regex_in_log(self):
        log = os.path.join(self.root, "job.log")
        self.write(log, "loading\n")
        self.later(1.5, lambda: self.write(log, "ALL DONE\n", "a"))
        self.run_guard(AUTOSTOP_DONE_FILE="", AUTOSTOP_DONE_LOG=sh_path(log), AUTOSTOP_DONE_REGEX="^ALL DONE$")
        self.assertTrue(self.status().startswith("DONE"), self.guard_log())

    def test_stuck_when_nothing_moves(self):
        self.run_guard()
        self.assertEqual(self.code, 0)
        self.assertTrue(self.status().startswith("STUCK"), self.status())
        self.assertLess(self.elapsed, 15)
        self.assertEqual(len(self.released()), 1)
        self.assertTrue(any(n.endswith("out/metrics.json") for n in self.archive_names()))

    def test_own_dir_and_ignored_paths_do_not_count_as_progress(self):
        # the guard writes its own log every poll (inside the watched tree), and Colab-like housekeeping
        # rewrites an ignored dir; neither may keep a dead job alive
        self.keep_writing(os.path.join(self.root, ".config", "gce"))
        self.keep_writing(os.path.join(self.box, "scratch.txt"))
        self.run_guard()
        self.assertTrue(self.status().startswith("STUCK"), self.guard_log())

    def test_progress_keeps_it_alive_until_cap(self):
        self.keep_writing(os.path.join(self.out, "ckpt.log"))
        self.run_guard(AUTOSTOP_CAP_SEC="7")
        self.assertTrue(self.status().startswith("CAP"), self.guard_log())
        self.assertGreaterEqual(self.elapsed, 6)  # bash $SECONDS counts whole seconds
        self.assertNotIn("STUCK", self.guard_log())

    def test_busy_gpu_counts_as_progress(self):
        self.run_guard(AUTOSTOP_CAP_SEC="6", FAKE_GPU="0\\n37")  # two GPUs, the second busy
        self.assertTrue(self.status().startswith("CAP"), self.guard_log())

    def test_gpu_below_threshold_is_idle(self):
        self.run_guard(FAKE_GPU="3")
        self.assertTrue(self.status().startswith("STUCK"), self.guard_log())

    def test_upload_failure_waits_then_releases(self):
        self.write(os.path.join(self.store, "fail_uploads"), "")
        self.run_guard()
        self.assertIsNone(self.status())
        self.assertEqual(len(self.released()), 1)
        log = self.guard_log()
        self.assertIn("upload failed", log)
        self.assertIn("try 2 of 2", log)
        self.assertTrue(os.path.exists(os.path.join(self.box, "results.tgz")))  # kept for `colab download`
        self.assertIn("releasing VM (STUCK, upload failed)", log)

    def test_no_upload_urls_still_releases(self):
        for n in ("put_results.url", "put_status.url"):
            os.remove(os.path.join(self.box, n))
        self.run_guard()
        self.assertEqual(len(self.released()), 1)
        self.assertIn("no upload configured", self.guard_log())
        self.assertNotIn("-T", self.read(self.store, "curl.calls"))

    def test_release_failure_retries_and_exits_2(self):
        self.write(os.path.join(self.store, "fail_release"), "")
        self.run_guard()
        self.assertEqual(self.code, 2)
        self.assertEqual(len(self.released()), 3)
        self.assertIn("could not release", self.guard_log())

    def test_not_armed_outside_colab(self):
        self.run_guard(TBE_RUNTIME_ADDR=None)
        self.assertEqual(self.code, 1)
        self.assertIn("guard not armed", self.guard_log())
        self.assertEqual(self.released(), [])

    JOB_SID = 4242

    def fake_cpu(self, job_pct, others_pct=0, every=0.25, with_pid_file=True):
        """Keep rewriting a fake /proc: the job's session (a leader and a child whose name has spaces and
        parentheses) uses self.cpu_level % of one core, a Colab-like service in another session uses others_pct."""
        self.cpu_level = job_pct
        proc = self.env["AUTOSTOP_PROC_DIR"]
        if with_pid_file:
            self.write(self.env["AUTOSTOP_JOB_PID_FILE"], f"{self.JOB_SID}\n")

        def stat(pid, comm, sid, ticks):
            os.makedirs(os.path.join(proc, str(pid)), exist_ok=True)
            fields = ["S", "1", str(sid), str(sid), "0", "-1", "0", "0", "0", "0", "0",
                      str(ticks // 2), str(ticks - ticks // 2), "0", "0", "20", "0", "1"]
            self.write(os.path.join(proc, str(pid), "stat"), f"{pid} ({comm}) " + " ".join(fields) + "\n")

        def loop():
            job = other = 0.0
            while not self.stop_writer.is_set():
                job += self.cpu_level * every       # CLK_TCK is 100, so % of one core = ticks per second
                other += others_pct * every
                stat(self.JOB_SID, "bash", self.JOB_SID, int(job / 2))
                stat(self.JOB_SID + 1, "python (worker) 1", self.JOB_SID, int(job - int(job / 2)))
                stat(100, "colab-fileshim", 1, int(other))
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()
        time.sleep(0.3)

    def test_busy_job_cpu_counts_as_progress(self):
        self.fake_cpu(80)  # 80 % of one core, e.g. single-threaded tokenizing that writes nothing
        self.run_guard(AUTOSTOP_CPU_BUSY_PCT="30", AUTOSTOP_CAP_SEC="6")
        self.assertTrue(self.status().startswith("CAP"), self.guard_log())

    def test_cpu_is_measured_per_poll_not_since_start(self):
        # busy for 4 s, then the job dies: STUCK must follow about IDLE seconds later, not once a
        # since-start average has decayed below the threshold (that would take about 14 s here)
        self.fake_cpu(80)
        self.later(4, lambda: setattr(self, "cpu_level", 0))
        self.run_guard(AUTOSTOP_CPU_BUSY_PCT="30", AUTOSTOP_CAP_SEC="30")
        self.assertTrue(self.status().startswith("STUCK"), self.guard_log())
        self.assertLess(self.elapsed, 11, self.guard_log())

    def test_other_processes_cpu_does_not_count(self):
        # what broke the first live run: an idle Colab VM's own services use 17-29 % of a core with spikes
        self.fake_cpu(5, others_pct=90)
        self.run_guard(AUTOSTOP_CPU_BUSY_PCT="30")
        self.assertTrue(self.status().startswith("STUCK"), self.guard_log())

    @unittest.skipUnless(os.path.exists("/proc/self/stat"), "needs a real /proc")
    def test_real_proc_busy_job_and_its_sleeping_twin(self):
        import sys
        for code, expect in (("while True: pass", "CAP"), ("import time; time.sleep(60)", "STUCK")):
            job = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            try:
                self.write(self.env["AUTOSTOP_JOB_PID_FILE"], f"{job.pid}\n")
                self.run_guard(AUTOSTOP_CPU_BUSY_PCT="30", AUTOSTOP_CAP_SEC="7", AUTOSTOP_PROC_DIR=None,
                               AUTOSTOP_CLK_TCK=None)
                self.assertTrue(self.status().startswith(expect), (code, self.guard_log()))
            finally:
                job.kill()
                job.wait()
            for n in ("status.txt", "released"):
                os.remove(os.path.join(self.store, n))

    def test_no_job_pid_means_no_cpu_signal(self):
        self.fake_cpu(80, with_pid_file=False)
        self.run_guard(AUTOSTOP_CPU_BUSY_PCT="30")
        self.assertTrue(self.status().startswith("STUCK"), self.guard_log())

    def test_hung_nvidia_smi_does_not_freeze_the_guard(self):
        self.run_guard(FAKE_GPU_HANG="1", AUTOSTOP_PROBE_TIMEOUT_SEC="1", AUTOSTOP_CAP_SEC="8")
        self.assertIn(self.status().split()[0], ("STUCK", "CAP"), self.guard_log())
        self.assertLess(self.elapsed, 30)

    def test_upload_urls_are_never_packed(self):
        # packing the whole watched tree must still leave out the guard dir (URLs, the archive itself)
        self.write(os.path.join(self.box, "job.exit"), "0\n")
        self.run_guard(AUTOSTOP_RESULTS=self.root)
        names = self.archive_names()
        self.assertTrue(any(n.endswith("out/metrics.json") for n in names), names)
        self.assertTrue(any(n == "guard.log" for n in names), names)
        self.assertFalse(any(n.endswith(".url") or n.endswith("results.tgz") for n in names), names)

    def test_non_numeric_marker_is_not_an_exit_code(self):
        self.write(os.path.join(self.box, "job.exit"), "2026-11-10T08:00:00Z\n")
        self.run_guard()
        self.assertNotIn("exit code", self.status())

    def test_env_file_argument(self):
        envfile = os.path.join(self.tmp, "autostop.env")
        with open(envfile, "w", newline="\n") as f:
            f.write("AUTOSTOP_IDLE_SEC=2\nAUTOSTOP_DONE_FILE=''\n")
        p = subprocess.run([BASH, sh_path(GUARD), sh_path(envfile)], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn("idle 2s", self.guard_log())
        self.assertTrue(self.status().startswith("STUCK"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(BASH is None, "needs bash on Linux/macOS/WSL (the guard runs on Linux)")
class BoxSideEndToEnd(unittest.TestCase):
    """Runs the launcher's real box-side files (starter, run_job.sh, env file, guard) in a temp dir as /content."""

    def test_starter_guard_and_job_reach_done(self):
        import argparse
        import sys

        from colab_autostop import cli
        from colab_autostop.sign import Storage

        tmp = tempfile.mkdtemp(prefix="autostop-box-")
        self.addCleanup(shutil.rmtree, tmp, True)
        content, store = os.path.join(tmp, "content"), os.path.join(tmp, "store")
        os.makedirs(os.path.join(content, "out"))
        os.makedirs(store)
        box = os.path.join(content, "autostop")
        a = argparse.Namespace(
            box_dir=box, job_log=os.path.join(content, "job.log"), no_exit_done=False, done_file=[],
            results=[os.path.join(content, "out")], cap_min=5, url_grace_hours=1, watch=[content], ignore=[],
            idle_min=2, poll_sec=1, gpu_busy_pct=5, cpu_busy_pct="30", done_log=None, done_regex=None, fallback_wait_min=0,
            cwd=content, job="echo training; sleep 2; echo 0.91 > out/acc.txt; echo finished")
        st = Storage("r2", "bkt", "AKID", "secret", account_id="acct")
        stage = os.path.join(content, "autostop-staging-")
        with tempfile.TemporaryDirectory() as t:
            files = cli.build_box_files(a, "jobs/e2e/20260101T000000Z", st, t)
            for name, path in files.items():
                shutil.copy(path, stage + name)
            starter = cli.BOX_STARTER.format(box_dir=box, stage=stage, names=sorted(files), cwd=content,
                                             armed_mark=cli.ARMED_MARK)
        env = dict(os.environ, PATH=FAKES + os.pathsep + os.environ["PATH"], FAKE_STORE=store,
                   TBE_RUNTIME_ADDR="10.0.0.1:9", FAKE_GPU="0")
        p = subprocess.run([sys.executable, "-c", starter], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn(cli.ARMED_MARK, p.stdout, p.stdout + p.stderr)
        released = os.path.join(store, "released")
        for _ in range(60):
            if os.path.exists(released):
                break
            time.sleep(0.5)
        with open(os.path.join(box, "guard.log")) as f:
            log = f.read()
        self.assertTrue(os.path.exists(released), log)
        with open(os.path.join(box, "job.pid")) as f:
            self.assertTrue(f.read().strip().isdigit())
        with open(os.path.join(store, "status.txt")) as f:
            self.assertRegex(f.read(), r"^DONE .*exit code 0")
        with tarfile.open(os.path.join(store, "results.tgz")) as t:
            names = t.getnames()
            self.assertTrue(any(n.endswith("out/acc.txt") for n in names), names)
            job_log = [n for n in names if n.endswith("job.log")][0]
            self.assertIn("finished", t.extractfile(job_log).read().decode())
