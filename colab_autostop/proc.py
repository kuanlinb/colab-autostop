"""Run a command with a hard time limit and kill its whole process tree when the limit passes.

subprocess.run(timeout=...) kills only the direct child. When the Colab CLI is started through a wrapper
(a shell or PowerShell script) the real CLI is a grandchild, keeps the output pipe open, and the call hangs anyway.
"""
import os
import signal
import subprocess
from dataclasses import dataclass


@dataclass
class Result:
    code: object  # int exit code, or None when the time limit killed it
    out: str

    @property
    def ok(self):
        return self.code == 0

    @property
    def timed_out(self):
        return self.code is None


def _kill_tree(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run(cmd, timeout):
    """Run `cmd` (a list), return Result. Never blocks much longer than `timeout` seconds."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return Result(p.returncode, out.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            out, _ = p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            out = b""
        return Result(None, out.decode("utf-8", "replace") + f"\n[killed after {timeout:g} s]")
