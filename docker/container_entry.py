"""Container entry; no implicit database or encryption-key creation."""
import os
import sys

if len(sys.argv) > 1:
    os.execvp(sys.argv[1], sys.argv[1:])
os.execv(sys.executable, [sys.executable, "/app/service/main.py"])
