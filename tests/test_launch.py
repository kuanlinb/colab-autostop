"""Launcher tests against a fake colab CLI. No Colab, no network, tested on Windows and Linux."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

from colab_autostop import cli, proc

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "fakes", "wrapper.py")
SECRET = "test-secret-key-do-not-leak"
# Time limit for each fake CLI call. A fake call normally takes well under a second, but two Python start-ups
# per call can take several seconds on a busy machine or a slow CI runner, so keep a wide margin.
LIMIT = 10
# A hung call must come back within LIMIT + KILL_SLACK. Killing the whole tree takes a few seconds; killing only
# the wrapper leaves the grandchild holding the pipe until the 15 s drain timeout, so it takes LIMIT + 15 or more.
KILL_SLACK = 8


class Launch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autostop-launch-")
        self.calls_log = os.path.join(self.tmp, "calls.jsonl")
        self.uploads = os.path.join(self.tmp, "uploads")
        os.makedirs(self.uploads)
        env = {
            "FAKE_COLAB_LOG": self.calls_log, "FAKE_COLAB_UPLOADS": self.uploads,
            "AUTOSTOP_PROVIDER": "r2", "AUTOSTOP_BUCKET": "results", "AUTOSTOP_ACCOUNT_ID": "acct",
            "AUTOSTOP_ACCESS_KEY_ID": "AKID", "AUTOSTOP_SECRET_ACCESS_KEY": SECRET,
        }
        for k in ("FAKE_COLAB_HANG", "FAKE_COLAB_NEW", "FAKE_COLAB_ARMED", "FAKE_COLAB_UPLOAD_FAIL"):
            env[k] = ""
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.state = mock.patch.object(cli, "STATE_DIR", os.path.join(self.tmp, "runs"))
        self.state.start()
        self.colab = f'"{sys.executable}" "{WRAPPER}"'

    def tearDown(self):
        self.state.stop()
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def launch(self, *extra, **env):
        os.environ.update(env)
        argv = ["launch", "--session", "t1", "--job", "python train.py --epochs 1", "--colab", self.colab,
                "--results", "/content/out", "--idle-min", "15", "--cap-min", "90", "--cli-timeout", str(LIMIT),
                "--new-timeout", str(LIMIT), "--upload-timeout", str(LIMIT)] + list(extra)
        out = io.StringIO()
        t0 = time.time()
        with contextlib.redirect_stdout(out):
            try:
                code = cli.main(argv)
            except SystemExit as e:
                code = e.code
        self.elapsed = time.time() - t0
        self.out = out.getvalue()
        return code

    def launch_timing_kills(self, **env):
        """launch(), also returning how long each CLI call that hit its time limit took to come back.
        Only those calls are timed: the total also holds the other fake CLI calls, which can each take a
        second or two on a busy machine. If only the wrapper died, the grandchild would hold the output
        pipe and the call would take the limit plus the 15 s drain timeout or more."""
        timed_out = []
        real_run = proc.run

        def timed_run(cmd, timeout):
            t0 = time.time()
            r = real_run(cmd, timeout)
            if r.timed_out:
                timed_out.append(time.time() - t0)
            return r

        with mock.patch.object(proc, "run", timed_run):
            code = self.launch(**env)
        return code, timed_out

    def calls(self):
        if not os.path.exists(self.calls_log):
            return []
        with open(self.calls_log) as f:
            return [json.loads(line) for line in f]

    def verbs(self):
        return [c[0] for c in self.calls()]

    def uploaded(self, name):
        with open(os.path.join(self.uploads, "content__autostop-staging-" + name), encoding="utf-8") as f:
            return f.read()

    def test_happy_path(self):
        code = self.launch("--gpu", "G4")
        self.assertEqual(code, 0, self.out)
        verbs = self.verbs()
        self.assertEqual(verbs[:3], ["sessions", "new", "sessions"])
        self.assertEqual(verbs.count("upload"), 6)
        self.assertEqual(verbs[-1], "exec")
        self.assertNotIn("stop", verbs)
        self.assertIn(["new", "-s", "t1", "--gpu", "G4"], self.calls())

        env = self.uploaded("autostop.env")
        self.assertIn("AUTOSTOP_IDLE_SEC=900\n", env)
        self.assertIn("AUTOSTOP_CAP_SEC=5400\n", env)
        self.assertIn("AUTOSTOP_DONE_FILE=/content/autostop/job.exit\n", env)
        self.assertIn("AUTOSTOP_RESULTS='/content/out /content/autostop-job.log'\n", env)
        self.assertIn("AUTOSTOP_IGNORE='/content/.config /content/drive /content/sample_data'\n", env)
        self.assertIn("AUTOSTOP_CPU_BUSY_PCT=30\n", env)
        self.assertIn("python train.py --epochs 1", self.uploaded("job_cmd.sh"))
        self.assertIn("PYTHONUNBUFFERED=1", self.uploaded("run_job.sh"))
        self.assertNotIn("\r", self.uploaded("run_job.sh"))

        with open(os.path.join(self.tmp, "runs", "t1.json")) as f:
            rec = json.load(f)
        put = urllib.parse.urlsplit(self.uploaded("put_results.url"))
        self.assertEqual(put.netloc, "acct.r2.cloudflarestorage.com")
        self.assertEqual(put.path, f"/results/{rec['key_prefix']}/results.tgz")
        self.assertTrue(self.uploaded("put_status.url").split("?")[0].endswith("/status.txt"))
        q = dict(urllib.parse.parse_qsl(put.query))
        self.assertEqual(q["X-Amz-Expires"], str(90 * 60 + 24 * 3600))  # cap + 24 h grace

        blob = json.dumps(rec)
        for n in os.listdir(self.uploads):
            with open(os.path.join(self.uploads, n), encoding="utf-8") as f:
                blob += f.read()
        self.assertNotIn(SECRET, blob)  # only signatures leave the laptop, never the key

    def test_hung_exec_is_killed_and_vm_stopped(self):
        # the wrapper starts the fake CLI as a grandchild, as a shell/PowerShell wrapper would
        code, timed_out = self.launch_timing_kills(FAKE_COLAB_HANG="exec")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.verbs()[-2:], ["stop", "sessions"])
        self.assertEqual(len(timed_out), 1, self.out)
        self.assertLess(timed_out[0], LIMIT + KILL_SLACK, "the whole process tree should die at the time limit")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "runs", "t1.json")))

    def test_guard_not_armed_stops_vm(self):
        code = self.launch(FAKE_COLAB_ARMED="0")
        self.assertIn("did not report armed", str(code))
        self.assertIn("stop", self.verbs())

    def test_upload_failure_stops_vm(self):
        code = self.launch(FAKE_COLAB_UPLOAD_FAIL="1")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.verbs().count("upload"), 1)
        self.assertIn("stop", self.verbs())
        self.assertNotIn("exec", self.verbs())

    def test_existing_session_name_is_refused(self):
        with open(self.calls_log + ".session", "w") as f:
            f.write("t1")  # an old run with the same name is still up
        code = self.launch()
        self.assertIn("already exists", str(code))
        self.assertEqual(self.verbs(), ["sessions"])

    def test_interrupt_after_renting_stops_vm(self):
        with mock.patch.object(cli, "build_box_files", side_effect=KeyboardInterrupt):
            code = self.launch()
        self.assertIn("interrupted (KeyboardInterrupt", str(code))
        self.assertEqual(self.verbs()[-2:], ["stop", "sessions"])

    def test_extra_ignore_keeps_colab_defaults(self):
        self.assertEqual(self.launch("--ignore", "/content/cache"), 0, self.out)
        self.assertIn("AUTOSTOP_IGNORE='/content/.config /content/drive /content/sample_data /content/cache'",
                      self.uploaded("autostop.env"))

    def test_keep_on_error_leaves_vm(self):
        self.launch("--keep-on-error", FAKE_COLAB_ARMED="0")
        self.assertNotIn("stop", self.verbs())

    def test_new_fails_without_vm(self):
        code = self.launch(FAKE_COLAB_NEW="fail")
        self.assertIn("colab new failed", str(code))
        self.assertEqual(self.verbs(), ["sessions", "new", "sessions"])

    def test_new_error_but_vm_assigned_is_used(self):
        code = self.launch(FAKE_COLAB_NEW="late")
        self.assertEqual(code, 0, self.out)
        self.assertEqual(self.verbs()[-1], "exec")

    def test_hung_new_is_killed_and_rechecked(self):
        with mock.patch.object(cli.time, "sleep") as sleep:
            code, timed_out = self.launch_timing_kills(FAKE_COLAB_HANG="new")
        # The patch replaces time.sleep everywhere, and on Linux subprocess's own wait loop also calls it,
        # so look only for the launcher's 30 s pause before it checks again.
        self.assertEqual(sleep.call_args_list.count(mock.call(30)), 1, sleep.call_args_list)
        self.assertIn("did not appear", str(code))
        self.assertEqual(self.verbs(), ["sessions", "new", "sessions", "sessions"])
        self.assertEqual(len(timed_out), 1, self.out)
        self.assertLess(timed_out[0], LIMIT + KILL_SLACK, "the whole process tree should die at the time limit")

    def test_missing_storage_fails_before_renting(self):
        os.environ["AUTOSTOP_SECRET_ACCESS_KEY"] = ""
        code = self.launch()
        self.assertIn("AUTOSTOP_SECRET_ACCESS_KEY", str(code))
        self.assertEqual(self.calls(), [])

    def test_rewritten_vm_paths_are_refused(self):
        # what Git Bash turns /content/out into when it starts a Windows python
        code = self.launch("--results", "C:/Program Files/Git/content/out")
        self.assertIn("MSYS_NO_PATHCONV", str(code))
        self.assertEqual(self.calls(), [])
        code = self.launch("--watch", "relative/dir")
        self.assertIn("absolute POSIX", str(code))

    def test_cap_longer_than_url_lifetime_is_refused(self):
        code = self.launch("--cap-min", str(7 * 24 * 60))
        self.assertIn("too long", str(code))
        self.assertEqual(self.calls(), [])


class Fetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autostop-fetch-")
        self.objects = {}
        self.env = mock.patch.dict(os.environ, {"AUTOSTOP_PROVIDER": "r2", "AUTOSTOP_BUCKET": "b",
                                                "AUTOSTOP_ACCOUNT_ID": "a", "AUTOSTOP_ACCESS_KEY_ID": "k",
                                                "AUTOSTOP_SECRET_ACCESS_KEY": "s"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_download(self, storage, key, dest):
        if key not in self.objects:
            return False
        with open(dest, "wb") as f:
            f.write(self.objects[key])
        return True

    def fetch(self, *extra):
        out = io.StringIO()
        with mock.patch.object(cli, "download", self.fake_download), contextlib.redirect_stdout(out):
            code = cli.main(["fetch", "--key", "p/x", "--dest", self.tmp] + list(extra))
        return code, out.getvalue()

    def test_not_finished(self):
        self.assertEqual(self.fetch()[0], 2)

    def test_done(self):
        self.objects = {"p/x/status.txt": b"DONE 2026-11-10T00:00:00Z done marker found, exit code 0\n",
                        "p/x/results.tgz": b"tgz"}
        code, out = self.fetch()
        self.assertEqual(code, 0)
        self.assertIn("status: DONE", out)
        with open(os.path.join(self.tmp, "p_x.results.tgz"), "rb") as f:
            self.assertEqual(f.read(), b"tgz")

    def test_done_with_failed_job_returns_4(self):
        self.objects = {"p/x/status.txt": b"DONE t done marker found, exit code 1\n", "p/x/results.tgz": b"x"}
        self.assertEqual(self.fetch()[0], 4)

    def test_stuck_returns_3(self):
        self.objects = {"p/x/status.txt": b"STUCK t no file written\n", "p/x/results.tgz": b"x"}
        self.assertEqual(self.fetch()[0], 3)

    def test_wait_polls_until_status(self):
        calls = {"n": 0}

        def sleep(_):
            calls["n"] += 1
            if calls["n"] == 2:
                self.objects = {"p/x/status.txt": b"CAP t\n", "p/x/results.tgz": b"x"}
        with mock.patch.object(cli.time, "sleep", sleep):
            code, out = self.fetch("--wait", "--poll-sec", "1")
        self.assertEqual(code, 3)
        self.assertEqual(calls["n"], 2)


class Proc(unittest.TestCase):
    def test_exit_code_and_output(self):
        r = proc.run([sys.executable, "-c", "print('hi'); raise SystemExit(3)"], 30)
        self.assertEqual((r.code, r.out.strip()), (3, "hi"))

    def test_timeout_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"FAKE_COLAB_LOG": os.path.join(d, "c"), "FAKE_COLAB_HANG": "exec"}):
            t0 = time.time()
            r = proc.run([sys.executable, WRAPPER, "exec"], 2)  # wrapper -> fake CLI that sleeps 120 s
            self.assertTrue(r.timed_out)
            self.assertLess(time.time() - t0, 12)


if __name__ == "__main__":
    unittest.main()
