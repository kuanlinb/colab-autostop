"""Fake colab CLI for the launcher tests. Appends each call to $FAKE_COLAB_LOG (JSON lines).

Behaviour comes from environment variables:
  FAKE_COLAB_HANG     subcommand that sleeps for 120 s instead of answering (e.g. "exec")
  FAKE_COLAB_NEW      "ok" (default), "fail", or "late" (new fails, but the session is listed anyway)
  FAKE_COLAB_ARMED    "1" (default): exec prints the armed mark; "0": prints NOT ARMED
  FAKE_COLAB_UPLOADS  directory where uploaded files are copied (named after the remote path)
  FAKE_COLAB_UPLOAD_FAIL  "1": every upload fails
"""
import json, os, shutil, sys, time

args = sys.argv[1:]
cmd = args[0] if args else ""
log = os.environ["FAKE_COLAB_LOG"]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\n")
state = log + ".session"
if os.environ.get("FAKE_COLAB_HANG") == cmd:
    time.sleep(120)
if cmd == "new":
    mode = (os.environ.get("FAKE_COLAB_NEW") or "ok")
    name = args[args.index("-s") + 1]
    if mode in ("ok", "late"):
        open(state, "w").write(name)
    if mode != "ok":
        print("Error: assignment failed"); sys.exit(1)
    print(f"[colab] Session '{name}' created")
elif cmd == "sessions":
    if os.path.exists(state):
        print(f"[{open(state).read()}] RUNNING")
    else:
        print("No active sessions.")
elif cmd == "upload":
    if os.environ.get("FAKE_COLAB_UPLOAD_FAIL") == "1":
        print("Error: 500 Server Error"); sys.exit(1)
    local, remote = args[-2], args[-1]
    up = os.environ.get("FAKE_COLAB_UPLOADS")
    if up:
        shutil.copy(local, os.path.join(up, remote.strip("/").replace("/", "__")))
    print("uploaded")
elif cmd == "exec":
    if (os.environ.get("FAKE_COLAB_ARMED") or "1") == "1":
        print("AUTOSTOP ARMED guard pid 11 job pid 12")
    else:
        print("AUTOSTOP NOT ARMED; job not started.")
elif cmd == "stop":
    if os.path.exists(state):
        os.remove(state)
    print("[colab] Session terminated.")
