"""Exercise shared backend imports in the source layout declared by Dockerfile.

No Docker engine, database, browser, or platform requests are used. Each service
gets a fresh subprocess with its own dependencies and current-service app path.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("backend-web", "websocket", "scheduler")


def copy_python_source(source, target):
    """Copy importable source only; never copy developer configs or credentials."""
    if source.is_file():
        if source.suffix == ".py":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return
    for directory, children, files in os.walk(source):
        children[:] = [name for name in children if not name.startswith(".") and name not in {
            "__pycache__", "node_modules", "logs", "tests", "static", "browser_data", "browser_runtime",
        }]
        relative = Path(directory).relative_to(source)
        for filename in files:
            if filename.endswith(".py"):
                destination = target / relative / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(directory) / filename, destination)


def copy_image_python_layout(service, image_root):
    """Use actual COPY declarations so removing shared source breaks this test."""
    for line in (ROOT / "docker/Dockerfile.app").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        parts = shlex.split(line.replace("${SERVICE}", service))
        sources, destination = parts[1:-1], parts[-1]
        if not destination.startswith("/app/"):
            continue
        target = image_root / destination.removeprefix("/app/")
        for source_name in sources:
            source = ROOT / source_name
            output = target / source.name if len(sources) > 1 or destination.endswith("/") else target
            copy_python_source(source, output)


IMPORT_PROBE = r'''
import asyncio
import json
from unittest.mock import AsyncMock, patch

from verify_runtime_modules import forbid_external_connections, verify_runtime_modules
forbid_external_connections()
loaded = verify_runtime_modules()

from common.services import xianyu_publish_service as publishing

# A real shared entry invokes the copied class without ever reaching a network.
capability = publishing.get_publish_account_capability_service_class()
async def probe():
    with patch.object(capability, "detect", new=AsyncMock(return_value={
        "success": True, "is_fish_shop": True, "detection_reliable": True,
    })) as detect:
        result = await publishing.detect_publish_account_capability(
            "synthetic-cookie", account_id="synthetic-account", owner_id=7,
        )
        assert result["is_fish_shop"]
        detect.assert_awaited_once_with(
            account_id="synthetic-account", cookie="synthetic-cookie", owner_id=7,
        )
asyncio.run(probe())
print(json.dumps(loaded))
'''


class SharedBackendImageLayoutTests(unittest.TestCase):
    def test_all_services_load_shared_entries_without_replacing_current_app(self):
        # One complete dependency environment checks all three package layouts;
        # the separately checked manifests install those same shared packages.
        self._probe_layouts(use_service_python=False)

    def test_service_environments_load_packaged_shared_entries(self):
        self._probe_layouts(use_service_python=True)

    def _probe_layouts(self, *, use_service_python):
        for service in SERVICES:
            with self.subTest(service=service), tempfile.TemporaryDirectory(prefix="ytx-image-layout-") as directory:
                image_root = Path(directory).resolve()
                copy_image_python_layout(service, image_root)
                data = image_root / "data"
                data.mkdir()
                python = ROOT / (service if use_service_python else "backend-web") / ".venv/bin/python"
                self.assertTrue(python.exists(), f"Prepare {service}'s declared dependencies first")
                if use_service_python:
                    probe = subprocess.run([
                        str(python), "-c", "import importlib.util,json; print(json.dumps([name for name in ('cv2','Crypto') if importlib.util.find_spec(name) is None]))",
                    ], text=True, capture_output=True, timeout=10)
                    self.assertEqual(probe.returncode, 0, probe.stderr)
                    missing = json.loads(probe.stdout)
                    if missing:
                        self.skipTest(f"{service} native venv needs updated requirements: {', '.join(missing)}")
                env = {
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join((str(image_root / "service"), str(image_root))),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "STATIC_DIR": str(image_root / "static"),
                }
                # The real key is neither needed nor exposed to an import probe.
                env.pop("YTX_KEYRING_FILE", None)
                env.pop("YTX_INSTANCE_ID", None)
                result = subprocess.run(
                    [str(python), "-c", IMPORT_PROBE], cwd=data, env=env,
                    text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-5000:])
                self.assertEqual(len(json.loads(result.stdout.splitlines()[-1])), 4)

    def test_shared_media_dependencies_are_declared_and_locked_consistently(self):
        import tomllib

        backend = (ROOT / "backend-web/requirements.lock").read_text().splitlines()
        required = {
            name: next(line for line in backend if line.startswith(name + "=="))
            for name in ("opencv-python-headless", "numpy", "pycryptodome")
        }
        for service in SERVICES:
            with self.subTest(service=service):
                lock = (ROOT / service / "requirements.lock").read_text().splitlines()
                for line in required.values():
                    self.assertIn(line, lock)
                project = tomllib.loads((ROOT / service / "pyproject.toml").read_text())
                for package in ("opencv-python-headless", "pycryptodome"):
                    self.assertTrue(any(value.startswith(package + ">=") for value in project["project"]["dependencies"]))


if __name__ == "__main__":
    unittest.main()
