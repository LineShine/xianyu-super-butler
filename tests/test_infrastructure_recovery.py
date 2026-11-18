"""Recover a verified Redis left behind by a failed stop without touching strangers."""
import errno
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from deployment import InfrastructureStack
from native_config import private_json, read_config
from native_infra import NativeInfrastructure, credentials


class InfrastructureRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ytx-redis-recovery-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.copytree(ROOT / 'config', self.root / 'config')
        self.stack = InfrastructureStack(self.root)
        self.stack.ensure_config()
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        with (self.root / '.env').open('a') as output:
            output.write(f'LOCAL_REDIS_PORT={self.listener.getsockname()[1]}\n')
        self.config = read_config(self.root)
        self.native = NativeInfrastructure(self.stack, self.config)
        env = credentials(self.config)
        identity = {key: env[key] for key in ('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD')}
        self.native.metadata = {
            'status': 'ready', 'redis_run_id': 'original-redis',
            'identity_hash': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        }
        self.client = MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.info.return_value = {
            'process_id': 1234, 'run_id': 'original-redis',
            'config_file': str(self.native.directory / 'redis.conf'),
        }
        self.client.config_get.return_value = {'dir': str(self.native.redis_dir)}

    def test_verified_orphan_saves_before_releasing_port(self):
        self.client.shutdown.side_effect = lambda **_: self.listener.close()
        with patch('native_infra.redis_connection', return_value=self.client):
            self.native.recover_redis(1234)
        self.client.shutdown.assert_called_once_with(save=True)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', int(self.config['services']['redis']['ports'][0]['published'])))

    def test_already_closed_orphan_does_not_connect_or_shutdown(self):
        self.listener.close()
        with patch('native_infra.redis_connection') as connect:
            self.native.recover_redis(1234)
        connect.assert_not_called()

    def test_reused_pid_run_id_or_different_paths_are_never_stopped(self):
        cases = [
            ({'process_id': 9999}, None),
            ({'run_id': 'replacement'}, None),
            ({'config_file': '/another/project/redis.conf'}, None),
            ({}, '/another/project/redis'),
        ]
        original = dict(self.client.info.return_value)
        for changes, directory in cases:
            with self.subTest(changes=changes, directory=directory):
                self.client.info.return_value = {**original, **changes}
                self.client.config_get.return_value = {'dir': directory or str(self.native.redis_dir)}
                with patch('native_infra.redis_connection', return_value=self.client):
                    with self.assertRaisesRegex(RuntimeError, '其他进程或实例'):
                        self.native.recover_redis(1234)
                self.client.shutdown.assert_not_called()

    def test_invalid_management_record_never_connects(self):
        for key, value in (('status', 'initializing'), ('redis_run_id', ''), ('identity_hash', 'other')):
            with self.subTest(key=key), patch.dict(self.native.metadata, {key: value}):
                with patch('native_infra.redis_connection') as connect:
                    with self.assertRaisesRegex(RuntimeError, '管理记录或数据库配置'):
                        self.native.recover_redis(1234)
                connect.assert_not_called()
        for pid in (None, True, 1, '1234'):
            with self.subTest(pid=pid), patch('native_infra.redis_connection') as connect:
                with self.assertRaisesRegex(RuntimeError, '管理记录或数据库配置'):
                    self.native.recover_redis(pid)
                connect.assert_not_called()

    def test_authentication_or_save_failure_never_forces_shutdown(self):
        for operation in ('info', 'shutdown'):
            with self.subTest(operation=operation):
                client = MagicMock()
                client.__enter__.return_value = client
                client.info.return_value = self.client.info.return_value
                client.config_get.return_value = self.client.config_get.return_value
                getattr(client, operation).side_effect = redis.ResponseError('fixture failure')
                with patch('native_infra.redis_connection', return_value=client):
                    with self.assertRaisesRegex(RuntimeError, '未强制结束'):
                        self.native.recover_redis(1234)
                if operation == 'info':
                    client.shutdown.assert_not_called()
                else:
                    client.shutdown.assert_called_once_with(save=True)

    def test_port_still_busy_after_shutdown_blocks_restart(self):
        with patch('native_infra.redis_connection', return_value=self.client), patch('native_infra.time.monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, '仍未释放端口'):
                self.native.recover_redis(1234)

    def test_bind_permission_error_is_not_a_port_conflict(self):
        with patch('native_infra.socket.socket') as factory:
            factory.return_value.__enter__.return_value.bind.side_effect = PermissionError(errno.EACCES, 'denied')
            with patch('native_infra.redis_connection') as connect:
                with self.assertRaises(PermissionError):
                    self.native.recover_redis(1234)
                connect.assert_not_called()

    def test_failed_redis_only_state_recovers_before_new_supervisor(self):
        previous = {'phase': 'failed', 'children': {'redis': 1234}}
        private_json(self.stack.state_path, previous)
        with patch.object(self.stack, 'owned_supervisor', return_value=False), \
                patch.object(self.stack, 'preflight_ports', side_effect=[None, RuntimeError('stop before spawn')]) as ports, \
                patch('native_infra.NativeInfrastructure.recover_redis') as recover, \
                patch('deployment.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'stop before spawn'):
                self.stack.start()
            self.assertEqual(set(ports.call_args_list[0].args[0]['services']), {'mysql'})
            recover.assert_called_once_with(1234)
            spawn.assert_not_called()
        self.assertEqual(json.loads(self.stack.state_path.read_text()), previous)

    def test_recovery_is_not_used_for_live_or_ambiguous_supervisors(self):
        for live, state in (
                (True, {'phase': 'running', 'children': {'redis': 1234}}),
                (False, {'phase': 'running', 'children': {'redis': 1234}}),
                (False, {'phase': 'failed', 'children': {'redis': 1234, 'mysql': 5678}})):
            with self.subTest(live=live, state=state):
                private_json(self.stack.state_path, state)
                with patch.object(self.stack, 'owned_supervisor', return_value=live), \
                        patch('deployment.check_native'), \
                        patch.object(self.stack, 'preflight_ports', side_effect=RuntimeError('occupied')), \
                        patch('native_infra.NativeInfrastructure.recover_redis') as recover:
                    if live:
                        self.stack.start()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'occupied'):
                            self.stack.start()
                    recover.assert_not_called()

    def test_running_mysql_blocks_orphan_recovery(self):
        private_json(self.stack.state_path, {'phase': 'failed', 'children': {'redis': 1234}})
        with patch.object(self.stack, 'owned_supervisor', return_value=False), \
                patch.object(self.stack, 'preflight_ports', side_effect=RuntimeError('mysql occupied')), \
                patch('native_infra.NativeInfrastructure.recover_redis') as recover:
            with self.assertRaisesRegex(RuntimeError, 'mysql occupied'):
                self.stack.start()
            recover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
