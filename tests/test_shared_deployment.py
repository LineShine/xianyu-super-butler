"""Preflight failures must leave the active applications and database untouched."""
import json
import os
from pathlib import Path
import shutil
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
        self.assertEqual(set(config["services"]), {"backend-web", "websocket", "scheduler", "frontend"})
        service = config["services"]["backend-web"]
        self.assertEqual(service["environment"]["MYSQL_HOST"], "host.docker.internal")
        self.assertEqual(service["environment"]["MYSQL_PORT"], "24306")
        self.assertEqual(service["environment"]["REDIS_DB"], "0")
        self.assertTrue(service["volumes"][0]["read_only"])
        self.assertFalse(any("native/mysql" in volume["source"] for volume in service["volumes"]))

    def test_docker_mode_never_reinitializes_native_instance_as_full(self):
        private_json(self.root / "yutaixian_data/instance.json", {"database_provider": "native"})
        with self.assertRaisesRegex(RuntimeError, "提供方式"):
            initialize_instance(self.root, provider="docker")


if __name__ == "__main__":
    unittest.main()
