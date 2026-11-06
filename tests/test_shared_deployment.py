"""Preflight failures must leave the active applications and database untouched."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from local_stack import LocalStack
from deployment import Deployment, InfrastructureStack, initialize_instance
from docker_deploy import DockerDeployment
from native_config import read_config, private_json
from common.security.vault import initialize_keyring


class SharedDeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ytx-deployment-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copytree(ROOT / "config", self.root / "config")
        self.stack = LocalStack(self.root)
        self.stack.ensure_config()
        self.manager = Deployment(self.stack)

    def test_lifecycles_have_distinct_states_and_ports(self):
        infra = InfrastructureStack(self.root)
        config = read_config(self.root)
        self.assertNotEqual(infra.state_path, self.stack.state_path)
        self.assertEqual(set(infra.ports(config)), {"mysql", "redis"})
        self.assertEqual(set(self.stack.ports(config)), {"backend-web", "websocket", "scheduler", "frontend"})

    def test_docker_unavailable_preserves_native_and_databases(self):
        docker = Mock()
        docker.prepare_build.side_effect = RuntimeError("Docker unavailable")
        with patch.object(self.manager, "docker", return_value=docker), patch.object(self.stack, "stop") as stop, patch.object(self.manager.infra, "stop") as stop_infra:
            with self.assertRaisesRegex(RuntimeError, "Docker unavailable"):
                self.manager.switch("docker")
            stop.assert_not_called()
            stop_infra.assert_not_called()

    def test_container_connection_preflight_failure_preserves_native(self):
        docker = Mock()
        docker.preflight_shared.side_effect = RuntimeError("wrong database")
        with patch.object(self.manager, "docker", return_value=docker), patch.object(self.stack, "prerequisites"), patch.object(self.manager.infra, "start"), patch("deployment.check_native"), patch("deployment.initialize_instance", return_value={"instance_id": "test"}), patch.object(self.stack, "stop") as stop:
            with self.assertRaisesRegex(RuntimeError, "wrong database"):
                self.manager.switch("docker")
            stop.assert_not_called()

    def test_missing_instance_key_never_replaced(self):
        config = read_config(self.root)["services"]["backend-web"]["environment"]
        key = self.root / "missing-key.json"
        private_json(self.root / "yutaixian_data/instance.json", {"database_provider": "native", "database_name": config["MYSQL_DATABASE"], "redis_db": config["REDIS_DB"], "keyring_file": str(key), "instance_id": "fixture"})
        with self.assertRaises(RuntimeError):
            initialize_instance(self.root)
        self.assertFalse(key.exists())

    def test_shared_compose_contains_only_apps_and_same_private_mounts(self):
        docker = DockerDeployment(self.stack)
        images = {name: "fixture-" + name for name in ("backend-web", "websocket", "scheduler", "frontend")}
        images["source_fingerprint"] = docker.fingerprint()
        private_json(self.stack.runtime / "images.json", images)
        private_json(self.root / "yutaixian_data/native/metadata.json", {"server_uuid": "fixture-mysql", "redis_run_id": "fixture-redis"})
        config = docker.config({"instance_id": "fixture", "keyring_file": "/outside/source/keyring.json"}, {"runtime_id": "one-generation"})
        self.assertEqual(config["name"], "ytx-fixture")
        self.assertEqual(set(config["services"]), {"backend-web", "websocket", "scheduler", "frontend"})
        service = config["services"]["backend-web"]
        self.assertEqual(service["environment"]["MYSQL_HOST"], "host.docker.internal")
        self.assertEqual(service["environment"]["MYSQL_PORT"], "24306")
        self.assertEqual(service["environment"]["REDIS_DB"], "0")
        self.assertTrue(service["volumes"][0]["read_only"])
        self.assertFalse(any("native/mysql" in volume["source"] for volume in service["volumes"]))
        named = docker.config({"instance_id": "fixture", "docker_project_name": "yutaixian", "keyring_file": "/outside/source/keyring.json"}, {"runtime_id": "one-generation"}, full=True)
        self.assertEqual(named["name"], "yutaixian")
        for service in named["services"].values():
            self.assertEqual(service["labels"]["org.yutaixian.instance"], "fixture")

    def test_docker_mode_never_reinitializes_native_instance_as_full(self):
        private_json(self.root / "yutaixian_data/instance.json", {"database_provider": "native"})
        with self.assertRaisesRegex(RuntimeError, "提供方式"):
            initialize_instance(self.root, provider="docker")

    def test_fingerprint_ignores_added_and_modified_os_metadata(self):
        source_dir = self.root / "backend-web" / "app"
        source_dir.mkdir(parents=True)
        (source_dir / "main.py").write_text("VERSION = 1\n")
        docker = DockerDeployment(self.stack)
        expected = docker.fingerprint()

        # Include both a service root and a nested source directory: opening either
        # in Finder/Explorer must not require another image build.
        for parent in (source_dir.parent, source_dir):
            for name in (".DS_Store", "._main.py", "Thumbs.db", "desktop.ini"):
                with self.subTest(directory=parent.name, metadata=name):
                    path = parent / name
                    path.write_bytes(b"initial desktop metadata")
                    self.assertEqual(docker.fingerprint(), expected)
                    path.write_bytes(b"updated desktop metadata")
                    self.assertEqual(docker.fingerprint(), expected)

    def test_fingerprint_tracks_source_and_dependency_changes(self):
        docker = DockerDeployment(self.stack)
        for relative in ("backend-web/app/main.py", "backend-web/requirements.lock", "frontend/src/App.tsx", "frontend/package-lock.json", "docker/Dockerfile.app"):
            with self.subTest(source=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                before = docker.fingerprint()
                path.write_text("initial build input\n")
                initial = docker.fingerprint()
                self.assertNotEqual(initial, before)
                path.write_text("updated build input\n")
                self.assertNotEqual(docker.fingerprint(), initial)

    def test_project_name_defaults_to_instance_and_accepts_explicit_ascii_name(self):
        instance = {"instance_id": "1234567890abcdef"}
        self.assertEqual(DockerDeployment.project_name(instance), "ytx-1234567890ab")
        for name in ("yutaixian", "a", "9-project_2"):
            with self.subTest(name=name):
                self.assertEqual(DockerDeployment.project_name({**instance, "docker_project_name": name}), name)

    def test_project_name_rejects_invalid_or_ambiguous_names(self):
        for name in ("", "鱼太咸", "Yutaixian", "-project", "_project", "a.b", "a b", "name\n", "１２３", None, 123):
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, "Docker 项目名称"):
                    DockerDeployment.project_name({"instance_id": "fixture", "docker_project_name": name})

    def test_project_name_accepts_unused_name_and_same_instance_containers(self):
        docker = DockerDeployment(self.stack)
        instance = {"instance_id": "fixture", "docker_project_name": "yutaixian"}
        result = lambda output: subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch("docker_deploy.subprocess.run", return_value=result("")) as run:
            docker.check_project_ownership(instance)
            self.assertEqual(run.call_count, 1)
        with patch("docker_deploy.subprocess.run", side_effect=[
            result("a" * 64 + "\n" + "b" * 64 + "\n"),
            result('["yutaixian", "fixture"]\n["yutaixian", "fixture"]\n'),
        ]) as run:
            docker.check_project_ownership(instance)
            listing = run.call_args_list[0].args[0]
            self.assertIn("--all", listing)  # Stopped containers also own the name.
            self.assertIn("label=com.docker.compose.project=yutaixian", listing)
            inspect = run.call_args_list[1].args[0]
            self.assertIn("--format", inspect)
            self.assertNotIn(".Env", " ".join(inspect))

    def test_foreign_or_unknown_project_containers_refused_before_manifest_write(self):
        docker = DockerDeployment(self.stack)
        instance = {"instance_id": "fixture", "docker_project_name": "yutaixian"}
        for label in (["yutaixian", "another-instance"], ["yutaixian", None], ["yutaixian", ""], ["different-project", "fixture"]):
            for entry in ("preflight_shared", "start_apps"):
                with self.subTest(labels=label, entry=entry):
                    manifest = self.stack.runtime / ("compose-preflight.json" if entry == "preflight_shared" else "compose.json")
                    manifest.write_text("existing manifest must remain intact")
                    with patch("docker_deploy.subprocess.run", side_effect=[
                        subprocess.CompletedProcess([], 0, stdout="a" * 64 + "\n", stderr=""),
                        subprocess.CompletedProcess([], 0, stdout=json.dumps(label) + "\n", stderr=""),
                    ]), patch.object(docker, "compose") as compose:
                        with self.assertRaisesRegex(RuntimeError, "拒绝接管"):
                            if entry == "preflight_shared":
                                docker.preflight_shared(instance)
                            else:
                                docker.start_apps(instance, {"runtime_id": "new"})
                        compose.assert_not_called()
                    self.assertEqual(manifest.read_text(), "existing manifest must remain intact")

    def test_project_name_check_fails_closed_when_docker_cannot_be_inspected(self):
        docker = DockerDeployment(self.stack)
        instance = {"instance_id": "fixture", "docker_project_name": "yutaixian"}
        listed = subprocess.CompletedProcess([], 0, stdout="a" * 64 + "\n", stderr="")
        cases = [
            [subprocess.CompletedProcess([], 1, stdout="", stderr="unavailable")],
            [subprocess.TimeoutExpired("docker", 20)],
            [listed, subprocess.CompletedProcess([], 1, stdout="", stderr="inspect failed")],
            [listed, subprocess.CompletedProcess([], 0, stdout="not json", stderr="")],
            [listed, subprocess.CompletedProcess([], 0, stdout="", stderr="")],
        ]
        for responses in cases:
            with self.subTest(responses=responses), patch("docker_deploy.subprocess.run", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "无法确认 Docker 项目名称"):
                    docker.check_project_ownership(instance)


if __name__ == "__main__":
    unittest.main()
