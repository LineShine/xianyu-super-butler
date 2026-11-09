"""Run business regressions with a temporary, synthetic instance key, never live keys."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.security.vault import initialize_keyring


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("services", nargs="*", default=["backend-web", "websocket", "scheduler"])
    args = parser.parse_args()
    failed = False
    with tempfile.TemporaryDirectory(prefix="ytx-regression-keys-") as directory:
        key = Path(directory) / "keyring.json"
        initialize_keyring(key, "synthetic-regression-instance")
        for service in args.services:
            if service not in ("backend-web", "websocket", "scheduler"):
                raise ValueError("Unknown service")
            env = {**os.environ, "PYTHONUTF8": "1", "YTX_KEYRING_FILE": str(key), "YTX_INSTANCE_ID": "synthetic-regression-instance",
                   "PYTHONPATH": os.pathsep.join((str(ROOT / service), str(ROOT)))}
            result = subprocess.run([str(ROOT / service / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")), "-m", "unittest", "discover", "-s", str(ROOT / service / "tests")],
                                    cwd=ROOT, env=env)
            failed = failed or result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
