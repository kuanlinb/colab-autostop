"""Starts the fake CLI as a child process, like a shell or PowerShell wrapper around the real colab CLI."""
import os, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.call([sys.executable, os.path.join(here, "colab_fake.py")] + sys.argv[1:]))
