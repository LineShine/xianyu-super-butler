"""Real Windows kernel tests; synthetic data only, no marketplace connections."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    from windows_process import Event, Job, Mutex, identity, matches, name_for
    from windows_setup import Setup, read_json, write_json, extract_zip, validate_entry
    from windows_runtime import Runtime
    from common.security.vault import initialize_keyring, read_keyring, encrypt, decrypt, CredentialUnavailable


@unittest.skipUnless(os.name == "nt", "Windows kernel integration tests")
class WindowsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ytx-中文 空格-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.env = os.environ.copy()
        self.env.update(PYTHONPATH=str(ROOT / "scripts") + os.pathsep + str(ROOT), PYTHONUTF8="1")

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail("timed out waiting for child state")

    def script(self, source):
        script = self.directory / ("程序 空格-" + uuid.uuid4().hex + ".py")
        script.write_text(source, encoding="utf-8")
        return script

    def test_browser_checksum_failure_never_publishes_an_installation(self):
        response = io.BytesIO(b"invalid archive bytes")
        response.url = "https://example.test/browser.zip"
        entry = {"name": "chromium", "version": "1248", "sha256": "0" * 64,
                 "source": response.url, "executable": "chrome-win64/chrome.exe"}
        setup = Setup(self.directory)
        with patch("windows_setup.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "SHA256"):
                setup.download(entry)
        self.assertFalse((setup.browsers / "chromium-1248").exists())

    def test_application_wall_clock_uses_beijing_time_on_windows(self):
        runtime = Runtime(self.directory)
        instance = {"instance_id": "synthetic", "keyring_file": "synthetic-key"}
        with patch.object(runtime, "check_existing_instance", return_value=instance), \
             patch.object(runtime, "config", return_value={}), \
             patch("local_dev.local_environment", return_value={"TZ": "Asia/Shanghai"}):
            env = runtime.app_environment("backend-web", "synthetic")
        actual = subprocess.check_output([sys.executable, "-c", "import time;print(time.timezone)"],
                                         env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(int(actual.strip()), -8 * 3600)

    def test_process_identity_rejects_reused_pid_or_wrong_image(self):
        own = identity(os.getpid())
        self.assertTrue(matches(own))
        self.assertFalse(matches({**own, "created": own["created"] + 1}))
        self.assertFalse(matches({**own, "image": own["image"] + ".wrong"}))
        self.assertFalse(matches({"pid": -1}))

    def test_job_closure_reaps_child_and_grandchild(self):
        record = self.directory / "tree.json"
        script = self.script("import os,sys,subprocess,time,json\nfrom windows_process import identity\n"
                             "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
                             "from windows_setup import write_json\nwrite_json(sys.argv[1],[identity(os.getpid()),identity(p.pid)])\n"
                             "time.sleep(120)\n")
        with (self.directory / "test.log").open("w") as output:
            job = Job(name_for(self.directory, "test", uuid.uuid4().hex))
            try:
                child = job.spawn([sys.executable, script, record], cwd=self.directory, env=self.env, output=output)
                self.wait_for(record.exists)
                records = read_json(record)
                self.assertTrue(all(matches(x) for x in records))
            finally:
                job.close()
            child.wait(10)
            child.close()
            self.wait_for(lambda: not any(matches(x) for x in records))

    def test_abrupt_supervisor_exit_closes_its_job(self):
        record = self.directory / "child.json"
        script = self.script("import sys,time,json\nfrom pathlib import Path\nfrom windows_process import Job,name_for\n"
                             "job=Job(name_for(Path(sys.argv[1]).parent,'crash'))\n"
                             "with open(sys.argv[1]+'.log','w') as out:\n"
                             " child=job.spawn([sys.executable,'-c','import time;time.sleep(120)'],cwd=Path(sys.argv[1]).parent,env=dict(__import__('os').environ),output=out)\n"
                             "from windows_setup import write_json\nwrite_json(sys.argv[1],child.identity)\n"
                             "time.sleep(120)\n")
        parent = subprocess.Popen([sys.executable, str(script), str(record)], env=self.env,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.wait_for(record.exists)
            child_record = read_json(record)
            self.assertTrue(matches(child_record))
        finally:
            parent.terminate()  # handle to the exact test parent
            parent.wait(10)
        self.wait_for(lambda: not matches(child_record))

    def test_event_shutdown_and_unicode_arguments(self):
        event_name = name_for(self.directory, "graceful", uuid.uuid4().hex)
        result = self.directory / "result.json"
        script = self.script("import sys,json\nfrom windows_process import Event\n"
                             "with Event(sys.argv[1],existing=True) as e:\n"
                             " assert e.wait(10000)\n"
                             "open(sys.argv[2],'w',encoding='utf-8').write(json.dumps(sys.argv[3:]))\n")
        values = ['中文 有空格', 'embedded"quote', 'trailing\\', '']
        with Event(event_name) as event, Job(name_for(self.directory, "unicode")) as job:
            with (self.directory / "test.log").open("w") as output:
                child = job.spawn([sys.executable, script, event_name, result, *values], cwd=self.directory, env=self.env, output=output)
            event.set()
            self.assertEqual(child.wait(15), 0)
            child.close()
            self.assertEqual(read_json(result), values)

    def test_named_mutex_blocks_other_process(self):
        name = name_for(self.directory, "mutex")
        with Mutex(name):
            result = subprocess.run([sys.executable, "-c", "import sys;from windows_process import Mutex;Mutex(sys.argv[1])", name],
                                    env=self.env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotEqual(result.returncode, 0)
        with Mutex(name):
            pass

    def test_key_acl_encryption_and_missing_key_fail_closed(self):
        key = self.directory / "secret" / "keyring.json"
        initialize_keyring(key, "test-instance")
        with patch.dict(os.environ, {"YTX_KEYRING_FILE": str(key), "YTX_INSTANCE_ID": "test-instance"}):
            value = encrypt("synthetic-password", ("account", 1))
            self.assertEqual(decrypt(value, ("account", 1)), "synthetic-password")
            with self.assertRaises(CredentialUnavailable):
                decrypt(value, ("account", 2))
            result = subprocess.run(["icacls.exe", str(key), "/grant", "*S-1-1-0:(F)"], capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(CredentialUnavailable):
                read_keyring()
            key.unlink()
            with self.assertRaises(CredentialUnavailable):
                decrypt(value, ("account", 1))
            self.assertFalse(key.exists())

    def test_archive_rejects_traversal_and_unverified_download(self):
        archive = self.directory / "bad.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escaped.txt", "unsafe")
        stage = self.directory / "stage"
        stage.mkdir()
        with self.assertRaises(RuntimeError):
            extract_zip(archive, stage)
        self.assertFalse((self.directory / "escaped.txt").exists())
        with self.assertRaises(RuntimeError):
            validate_entry({"sha256": None, "source": "https://example.test/file"})
        with self.assertRaises(RuntimeError):
            validate_entry({"sha256": "a" * 64, "source": "http://example.test/file"})

    def test_existing_data_without_env_never_generates_passwords(self):
        runtime = Runtime(self.directory)
        runtime.ensure_directories()
        (runtime.data / "native/metadata.json").write_text('{}')
        with self.assertRaises(RuntimeError):
            runtime.ensure_config()
        self.assertFalse((self.directory / ".env").exists())

    def test_stale_owner_cannot_signal_a_live_unrelated_process(self):
        runtime = Runtime(self.directory)
        runtime.ensure_directories()
        state = {"platform": "win32", "root": str(runtime.root), "group": "apps", "token": "old",
                 "owner": {**identity(os.getpid()), "created": 0}, "children": {}, "phase": "running"}
        write_json(runtime.path_for("apps"), state)
        with patch("windows_runtime.Event") as event:
            runtime.stop_group("apps")
            event.assert_not_called()
        self.assertTrue(matches(identity(os.getpid())))

    def test_port_conflict_does_not_stop_listener(self):
        runtime = Runtime(self.directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            config = {"services": {name: {"ports": [{"published": listener.getsockname()[1]}]} for name in ("mysql", "redis")}}
            with patch.object(runtime, "config", return_value=config):
                with self.assertRaises(RuntimeError):
                    runtime.preflight_ports("infra")
            with socket.create_connection(listener.getsockname(), timeout=1):
                pass

    def test_corrupt_state_is_not_silently_treated_as_fresh_install(self):
        state = self.directory / "state.json"
        state.write_text('{broken')
        with self.assertRaises(ValueError):
            read_json(state)


if __name__ == "__main__":
    unittest.main()
