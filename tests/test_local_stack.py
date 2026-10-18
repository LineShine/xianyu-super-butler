"""Ownership and failure handling of the native launcher."""
from pathlib import Path
import json
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from local_stack import LocalStack
from native_config import read_config,private_json

class LocalStackTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='ytx-test-');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);shutil.copytree(ROOT/'config',self.root/'config')
        self.stack=LocalStack(self.root);self.stack.ensure_config()
    def test_config_literals_and_isolation(self):
        with (self.root/'.env').open('a') as out:out.write("MYSQL_PASSWORD='a$#${literal} secret'\nREDIS_DB=3\n")
        with patch('subprocess.run',side_effect=AssertionError('No container inspection')):
            config=read_config(self.root)
        self.assertEqual(config['services']['backend-web']['environment']['MYSQL_PASSWORD'],'a$#${literal} secret')
        self.assertEqual(self.stack.ports(config),{'mysql':24306,'redis':27379,'backend-web':8189,'websocket':8190,'scheduler':8191,'frontend':9100})
    def test_port_conflict_stops_before_spawning(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            config={'services':{'frontend':{'ports':[{'target':80,'published':str(listener.getsockname()[1])}]}}}
            with self.assertRaisesRegex(RuntimeError,'占用'):self.stack.preflight_ports(config,{})
    def test_stale_pid_never_signalled(self):
        private_json(self.stack.state_path,{'pid':123,'token':'foreign'})
        with patch('subprocess.run',return_value=Mock(returncode=0,stdout='unrelated process')),patch('os.kill') as kill:
            self.stack.stop();kill.assert_not_called()
    def test_incomplete_migration_prevents_all_workers(self):
        private_json(self.stack.runtime/'data-migration.json',{'phase':'failed'})
        with patch.object(self.stack,'prepare') as prepare:
            with self.assertRaisesRegex(RuntimeError,'迁移尚未'):self.stack.start(open_browser=False)
            prepare.assert_not_called()
    def test_existing_runtime_without_credentials_refuses_regeneration(self):
        (self.root/'.env').unlink();private_json(self.root/'yutaixian_data/native/metadata.json',{'status':'ready'})
        with self.assertRaisesRegex(RuntimeError,'缺少'):self.stack.ensure_config()
    def test_repeated_start_is_idempotent(self):
        private_json(self.stack.state_path,{'phase':'running','url':'http://127.0.0.1:9100'})
        with patch.object(self.stack,'owned_supervisor',return_value=True),patch.object(self.stack,'prepare') as prepare:
            self.stack.start(open_browser=False);prepare.assert_not_called()
