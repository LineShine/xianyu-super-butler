"""Private native MySQL/Redis instances, owned by the local supervisor."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import pymysql
import redis
from native_config import private_json

def identifier(value: str) -> str:
    return '`' + value.replace('`', '``') + '`'

def credentials(config):
    return config['services']['backend-web']['environment']

def port(config, name):
    return int(config['services'][name]['ports'][0]['published'])

def mysql_connection(config, **kwargs):
    env = credentials(config)
    return pymysql.connect(host='127.0.0.1', port=port(config, 'mysql'), user=env['MYSQL_USER'], password=env['MYSQL_PASSWORD'], database=env['MYSQL_DATABASE'], charset='utf8mb4', connect_timeout=3, read_timeout=60, **kwargs)

def redis_connection(config, **kwargs):
    env = credentials(config)
    kwargs.setdefault('single_connection_client', True)
    return redis.Redis(host='127.0.0.1', port=port(config, 'redis'), password=env.get('REDIS_PASSWORD') or None, socket_connect_timeout=3, socket_timeout=10, **kwargs)

class NativeInfrastructure:

    def __init__(self, stack, config):
        (self.stack, self.config) = (stack, config)
        self.root = stack.root
        self.directory = self.root / 'yutaixian_data/native'
        self.directory.mkdir(parents=True, exist_ok=True, mode=448)
        self.metadata_path = self.directory / 'metadata.json'
        self.metadata = json.loads(self.metadata_path.read_text()) if self.metadata_path.exists() else {}
        self.tools_path = stack.runtime / 'native-tools.json'
        self.tools = json.loads(self.tools_path.read_text()) if self.tools_path.exists() else {}
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.socket_directory = Path(tempfile.gettempdir()) / f'ytx-{os.getuid()}-{digest}'
        self.socket_directory.mkdir(exist_ok=True, mode=448)
        self.socket = self.socket_directory / 'mysql.sock'
        self.mysql_dir = self.directory / 'mysql'
        self.redis_dir = self.directory / 'redis'

    def save(self):
        private_json(self.metadata_path, self.metadata)

    def prepare_tools(self):
        brew = shutil.which('brew')
        if not brew:
            raise RuntimeError('未找到 Homebrew，请通过 bash dev.sh 启动以自动准备运行环境。')
        prefix = Path(subprocess.check_output([brew, '--prefix'], text=True).strip())
        mysql_bin = prefix / 'opt/mysql@8.4/bin'
        redis_bin = prefix / 'opt/redis/bin'
        missing = []
        if not (mysql_bin / 'mysqld').is_file():
            missing.append('mysql@8.4')
        if not (redis_bin / 'redis-server').is_file():
            missing.append('redis')
        node = shutil.which('node')
        if not node or int(subprocess.check_output([node, '-p', "process.versions.node.split('.')[0]"], text=True)) < 18:
            missing.append('node@22')
            node = str(prefix / 'opt/node@22/bin/node')
        if missing:
            try:
                self.stack.command([brew, 'install', *missing], label='自动安装本机组件：' + '、'.join(missing))
            except RuntimeError:
                if os.environ.get('HOMEBREW_BOTTLE_DOMAIN'):
                    raise
                environment = os.environ.copy()
                environment['HOMEBREW_BOTTLE_DOMAIN'] = 'https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles'
                self.stack.command([brew, 'install', *missing], env=environment, label='官方源安装未完成，使用清华镜像重试（保留 Homebrew 校验）')
        self.tools = {name: str(mysql_bin / name) for name in ('mysqld', 'mysql', 'mysqldump')}
        self.tools.update({name: str(redis_bin / name) for name in ('redis-server', 'redis-cli')})
        self.tools.update(node=node, npm=str(Path(node).parent / 'npm'))
        for (name, path) in self.tools.items():
            if not Path(path).is_file():
                raise RuntimeError(f'{name} 安装未完成，请查看 setup.log 后重试。')
        private_json(self.tools_path, self.tools)
        os.environ['PATH'] = os.pathsep.join((str(Path(node).parent), str(mysql_bin), str(redis_bin), str(prefix / 'bin'), os.environ['PATH']))

    def admin(self, *, password=None):
        return pymysql.connect(unix_socket=str(self.socket), user='root', password=self.metadata['admin_password'] if password is None else password, charset='utf8mb4', connect_timeout=2, read_timeout=60, autocommit=True)

    def mysql_args(self, *, bootstrap=False):
        args = [self.tools['mysqld'], '--no-defaults', '--basedir=' + str(Path(self.tools['mysqld']).parent.parent), '--datadir=' + str(self.mysql_dir), '--socket=' + str(self.socket), '--pid-file=' + str(self.directory / 'mysql.pid'), '--mysqlx=0', '--skip-log-bin', '--character-set-server=utf8mb4', '--collation-server=utf8mb4_unicode_ci', '--default-time-zone=+08:00', '--max-allowed-packet=256M', '--max-connections=4000']
        if bootstrap:
            args.append('--skip-networking')
        else:
            args += ['--bind-address=127.0.0.1', f"--port={port(self.config, 'mysql')}"]
        return args

    def spawn(self, name, args):
        with self.stack.log_file(name) as output:
            process = subprocess.Popen(args, cwd=self.directory, stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        self.stack.children.append((name, process))
        return process

    def wait_mysql(self, process, *, allow_empty=False):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None or self.stack.stopping:
                raise RuntimeError('本机 MySQL 未能启动，请查看 mysql.log。')
            if not self.socket.exists():
                time.sleep(0.3)
                continue
            for password in (None, '') if allow_empty else (None,):
                try:
                    connection = self.admin(password=password)
                    return connection
                except pymysql.MySQLError:
                    pass
            time.sleep(0.3)
        raise RuntimeError('本机 MySQL 就绪超时，请查看 mysql.log。')

    def stop_one(self, process, timeout=90):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
        self.stack.children = [(name, child) for (name, child) in self.stack.children if child is not process]

    def initialize_mysql(self):
        env = credentials(self.config)
        identity = {key: env[key] for key in ('MYSQL_DATABASE', 'MYSQL_USER', 'MYSQL_PASSWORD')}
        identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if self.metadata.get('identity_hash') and self.metadata['identity_hash'] != identity_hash:
            raise RuntimeError('根目录数据库凭证与已初始化的本机库不一致，请恢复原 .env 后重试。')
        if not self.metadata:
            if self.mysql_dir.exists() and any(self.mysql_dir.iterdir()):
                raise RuntimeError('发现没有管理记录的本机 MySQL 数据，已停止以防覆盖。')
            self.metadata = {'admin_password': secrets.token_hex(32), 'identity_hash': identity_hash, 'status': 'initializing'}
            self.save()
        if not self.metadata.get('mysql_initialized'):
            if self.mysql_dir.exists() and (not (self.mysql_dir / 'mysql.ibd').exists()) and any(self.mysql_dir.iterdir()):
                self.mysql_dir.rename(self.directory / ('incomplete-mysql-' + secrets.token_hex(6)))
            if not (self.mysql_dir / 'mysql.ibd').exists():
                self.mysql_dir.mkdir(exist_ok=True, mode=448)
                self.stack.command([self.tools['mysqld'], '--no-defaults', '--initialize-insecure', '--datadir=' + str(self.mysql_dir), '--basedir=' + str(Path(self.tools['mysqld']).parent.parent)], label='初始化独立的本机 MySQL 数据目录')
            process = self.spawn('mysql', self.mysql_args(bootstrap=True))
            with self.wait_mysql(process, allow_empty=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (self.metadata['admin_password'],))
                    cursor.execute('CREATE DATABASE IF NOT EXISTS ' + identifier(env['MYSQL_DATABASE']) + ' CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
                    for host in ('localhost', '127.0.0.1', '%'):
                        cursor.execute('CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s', (env['MYSQL_USER'], host, env['MYSQL_PASSWORD']))
                        cursor.execute('ALTER USER %s@%s IDENTIFIED BY %s', (env['MYSQL_USER'], host, env['MYSQL_PASSWORD']))
                        cursor.execute('GRANT ALL ON ' + identifier(env['MYSQL_DATABASE']) + '.* TO %s@%s', (env['MYSQL_USER'], host))
            self.stop_one(process)
            self.metadata['mysql_initialized'] = True
            self.save()

    def start(self):
        self.initialize_mysql()
        mysql = self.spawn('mysql', self.mysql_args())
        with self.wait_mysql(mysql) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT @@server_uuid')
                self.metadata['server_uuid'] = cursor.fetchone()[0]
        self.redis_dir.mkdir(exist_ok=True, mode=448)
        env = credentials(self.config)
        configuration = self.directory / 'redis.conf'
        configuration.write_text('\n'.join(('bind 127.0.0.1', f"port {port(self.config, 'redis')}", 'protected-mode yes', 'daemonize no', 'dir ' + json.dumps(str(self.redis_dir)), 'dbfilename dump.rdb', 'requirepass ' + json.dumps(env.get('REDIS_PASSWORD', '')), 'appendonly ' + 'yes', 'appendfsync everysec', 'save 60 1', 'maxmemory 256mb', 'maxmemory-policy allkeys-lru', '')))
        os.chmod(configuration, 384)
        process = self.spawn('redis', [self.tools['redis-server'], str(configuration)])
        deadline = time.monotonic() + 90
        client = None
        while time.monotonic() < deadline:
            if process.poll() is not None or self.stack.stopping:
                raise RuntimeError('本机 Redis 启动失败，请查看 redis.log。')
            try:
                if client is None:
                    client = redis_connection(self.config)
                if client.ping():
                    break
            except redis.RedisError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError('本机 Redis 就绪超时，请查看 redis.log。')
        self.metadata.update(status='ready', redis_run_id=client.info('server')['run_id'])
        self.save()
        client.close()

def check_native(config):
    directory = Path(config['services']['mysql']['volumes'][0]['source']).parent
    path = directory / 'metadata.json'
    if not path.is_file():
        raise RuntimeError('本机基础服务尚未初始化，请执行 bash dev.sh start。')
    metadata = json.loads(path.read_text())
    if metadata.get('status') != 'ready':
        raise RuntimeError('本机数据准备尚未完成，不能启动应用。')
    with mysql_connection(config) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT @@server_uuid')
            if cursor.fetchone()[0] != metadata['server_uuid']:
                raise RuntimeError('MySQL 端口对应的实例与本机数据不一致。')
    with redis_connection(config) as client:
        if client.info('server')['run_id'] != metadata['redis_run_id']:
            raise RuntimeError('Redis 端口对应的实例与本机数据不一致。')
