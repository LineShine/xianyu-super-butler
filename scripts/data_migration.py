"""Offline, resumable import into an independent instance. Never rewrites SQL data."""
from __future__ import annotations

import ast
import base64
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import time

import pymysql
import redis
from dotenv import dotenv_values
from native_config import private_json, read_config
from native_infra import NativeInfrastructure, credentials, identifier, mysql_connection, redis_connection

EXCLUDED_USER_KEYS = {'balance', 'payment_qrcode', 'card_secret_key', 'dock_code'}
VOLATILE_PREFIXES = ('lock:', 'session:', 'auth:', 'token:', 'heartbeat:', 'running:', 'captcha:')
FILE_SKIP = {'SingletonLock', 'SingletonSocket', 'SingletonCookie', 'DevToolsActivePort', '.DS_Store', 'xianyu-group.png', 'wechat-group.png', 'wechat-group1.png', 'qq-group.png', 'wechat.png'}


def load(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    if isinstance(value, (bytes, bytearray)):
        return {'bytes': base64.b64encode(value).decode()}
    if isinstance(value, (date, datetime)):
        return {'date': value.isoformat()}
    if isinstance(value, Decimal):
        return {'decimal': str(value)}
    return value


def rows_digest(rows):
    """Multiset digest; independent of storage order, preserves duplicate counts."""
    hashes = sorted(hashlib.sha256(json.dumps([canonical(v) for v in row], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest() for row in rows)
    return hashlib.sha256(''.join(hashes).encode()).hexdigest()


def literal(path, name):
    for node in ast.walk(ast.parse(Path(path).read_text())):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError(f'缺少初始化定义：{name}')


def source_info(root):
    root = Path(root).resolve()
    if not (root / '.env').is_file():
        raise RuntimeError('来源目录缺少 .env。')
    # This is the only compatibility knowledge about the original layout.
    data = root / 'xianyu_auto_reply'
    metadata = load(data / 'native/metadata.json')
    tools = load(data / 'local_dev/native-tools.json')
    if not metadata.get('mysql_initialized') or not tools.get('mysqld'):
        raise RuntimeError('来源必须是已经初始化的原生方式五实例。')
    env = {k: v for k, v in dotenv_values(root / '.env', interpolate=False).items() if v is not None}
    return root, data, env, metadata, tools


def source_connection(env):
    user = env.get('MYSQL_USER', 'root')
    if user == 'root':
        user = 'xianyu_local'
    return pymysql.connect(host='127.0.0.1', port=int(env.get('LOCAL_MYSQL_PORT', '23306')), user=user,
        password=env.get('MYSQL_PASSWORD', ''), database=env.get('MYSQL_DATABASE', 'xianyu_data'),
        charset='utf8mb4', autocommit=True, read_timeout=120)


def inventory(connection, mapping):
    with connection.cursor() as cursor:
        cursor.execute('SHOW TABLES')
        tables = [row[0] for row in cursor.fetchall()]
        unknown = set(tables) - set(mapping['tables']) - set(mapping['excluded_tables'])
        if unknown:
            raise RuntimeError('发现未定义迁移规则的表：' + ', '.join(sorted(unknown)))
        report = []
        for table in tables:
            cursor.execute('SELECT COUNT(*) FROM ' + identifier(table))
            report.append({'source': table, 'target': mapping['tables'].get(table), 'rows': cursor.fetchone()[0],
                           'action': 'retain' if table in mapping['tables'] else 'exclude'})
        # Distribution data is not silently converted to self-owned stock.
        if 'xy_card_item_relations' in tables:
            cursor.execute("SELECT COUNT(*) FROM xy_card_item_relations WHERE COALESCE(source,'own')<>'own' OR COALESCE(dock_record_id,0)<>0")
            if cursor.fetchone()[0]:
                raise RuntimeError('来源存在对接卡券关联，纯净版不能将其当作自有库存导入。')
        return report


def filtered_setting(key, mapping):
    return key in EXCLUDED_USER_KEYS or key.startswith(tuple(mapping['excluded_setting_prefixes']))


def transform_row(table, columns, row, mapping, overrides):
    result = dict(zip(columns, row))
    if table.endswith('_settings'):
        key = result['key']
        if filtered_setting(key, mapping):
            return None
        if table == 'xy_system_settings' and key in overrides:
            result['value'] = overrides[key]
            result['description'] = '鱼太咸实例配置'
    if table == 'xy_db_backup_log' and result.get('file_name'):
        result['file_name'] = f"ytx_archive_{result['id']}.sql.gz"
        result['file_path'] = 'backups/' + result['file_name']
    if table == 'xy_card_item_relations':
        result['source'] = 'own'
    return tuple(result[c] for c in columns)


def redis_key(key):
    text = key.decode('utf-8', errors='strict')
    unprefixed = text[4:] if text.startswith('ytx:') else text
    if unprefixed.startswith(VOLATILE_PREFIXES):
        return None
    return ('ytx:' + unprefixed).encode()


def assert_ports_closed(env):
    for key, default in [('LOCAL_MYSQL_PORT',23306),('LOCAL_REDIS_PORT',26379),('BACKEND_WEB_PORT',8089),('WEBSOCKET_PORT',8090),('SCHEDULER_PORT',8091)]:
        with socket.socket() as probe:
            probe.settimeout(.3)
            if probe.connect_ex(('127.0.0.1', int(env.get(key,default)))) == 0:
                raise RuntimeError(f'原实例端口 {env.get(key,default)} 仍在监听，尚不能制作停写快照。')


@contextmanager
def snapshot_services(backup, tools, metadata, stack):
    """Boot disposable copies over private Unix sockets. Pristine snapshot is read-only."""
    work = backup / 'export-working'
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(backup / 'native', work, symlinks=True)
    sockets = Path(tempfile.mkdtemp(prefix='ytx-export-'))
    children = []
    output = stack.log_file('migration')
    try:
        mysql_socket = sockets / 'mysql.sock'
        args = [tools['mysqld'], '--no-defaults', '--basedir=' + str(Path(tools['mysqld']).parent.parent),
                '--datadir=' + str(work / 'mysql'), '--socket=' + str(mysql_socket), '--pid-file=' + str(work / 'mysql.pid'),
                '--skip-networking', '--mysqlx=0', '--skip-log-bin', '--default-time-zone=+08:00', '--max-allowed-packet=256M']
        mysql = subprocess.Popen(args, stdout=output, stderr=output, start_new_session=True)
        children.append(mysql)
        db = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if mysql.poll() is not None:
                raise RuntimeError('备份副本 MySQL 启动失败，原库未被修改。')
            try:
                db = pymysql.connect(unix_socket=str(mysql_socket), user='root', password=metadata['admin_password'], charset='utf8mb4', autocommit=True, read_timeout=120)
                break
            except pymysql.MySQLError:
                time.sleep(.3)
        if db is None:
            raise RuntimeError('备份副本 MySQL 启动超时。')
        redis_socket = sockets / 'redis.sock'
        args = [tools['redis-server'], '--port', '0', '--unixsocket', str(redis_socket), '--unixsocketperm', '700',
                '--dir', str(work / 'redis'), '--appendonly', 'yes', '--save', '', '--daemonize', 'no']
        redis_process = subprocess.Popen(args, stdout=output, stderr=output, start_new_session=True)
        children.append(redis_process)
        client = redis.Redis(unix_socket_path=str(redis_socket), socket_timeout=5)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if redis_process.poll() is not None:
                raise RuntimeError('备份副本 Redis 启动失败。')
            try:
                if client.ping(): break
            except redis.RedisError: time.sleep(.2)
        else: raise RuntimeError('备份副本 Redis 启动超时。')
        with db, client:
            yield db, client
    finally:
        for process in reversed(children):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=120)
        output.close()
        shutil.rmtree(sockets, ignore_errors=True)
        if work.exists(): shutil.rmtree(work)


def initialize_schema(connection, root):
    """Declarative schema only: no accounts, jobs or startup side effects."""
    ddl = literal(root / 'common/db/init_database.py', 'TABLES_DDL')
    ddl.update(literal(root / 'common/db/auto_relist_schema.py', 'AUTO_RELIST_TABLE_DDL'))
    migrations = literal(root / 'common/db/init_database.py', 'COLUMN_MIGRATIONS')
    for table, columns in literal(root / 'common/db/auto_relist_schema.py', 'AUTO_RELIST_COLUMNS').items():
        migrations.setdefault(table, []).extend((name, definition, '') for name, definition in columns.items())
    migrations.setdefault('ytx_publish_logs', []).extend([('publish_request_id','VARCHAR(100) NULL',''),('source_event_id','BIGINT NULL','')])
    with connection.cursor() as cursor:
        for sql in ddl.values(): cursor.execute(sql)
        for table, changes in migrations.items():
            if table not in ddl: continue
            cursor.execute('SHOW COLUMNS FROM ' + identifier(table)); existing = {row[0] for row in cursor.fetchall()}
            for name, definition, after in changes:
                if name not in existing:
                    cursor.execute('ALTER TABLE ' + identifier(table) + ' ADD COLUMN ' + identifier(name) + ' ' + definition)
                    existing.add(name)
    connection.commit()
    return set(ddl)


def verify_unused(connection, state):
    with connection.cursor() as cursor:
        cursor.execute('SHOW TABLES'); tables = {row[0] for row in cursor.fetchall()}
        if state.get('snapshot_complete') and state.get('phase') not in ('complete','activated'):
            return
        for table in ('ytx_accounts','ytx_orders','ytx_cards','ytx_catalog_items','ytx_keyword_rules'):
            if table in tables:
                cursor.execute('SELECT COUNT(*) FROM ' + identifier(table))
                if cursor.fetchone()[0]: raise RuntimeError('新实例已经含有业务数据，禁止重复导入覆盖。')
        if 'ytx_users' in tables:
            cursor.execute('SELECT COUNT(*) FROM ytx_users')
            if cursor.fetchone()[0] > 1: raise RuntimeError('新实例已经含有用户数据，禁止覆盖。')
            cursor.execute("SELECT COUNT(*) FROM ytx_users WHERE username<>'admin' OR last_login_at IS NOT NULL")
            if cursor.fetchone()[0]: raise RuntimeError('新实例已被用户使用，禁止覆盖。')


def import_tables(source, destination, mapping, state, config, root):
    tables = inventory(source, mapping)
    overrides = state['setting_overrides']
    with destination.cursor() as cleanup:
        cleanup.execute("SHOW TABLES")
        old_tables = [r[0] for r in cleanup.fetchall()]
        if any(not t.startswith("ytx_") for t in old_tables):
            raise RuntimeError("目标库包含其他应用的数据，拒绝覆盖。")
        cleanup.execute("SET FOREIGN_KEY_CHECKS=0")
        for old_table in old_tables:
            cleanup.execute("DROP TABLE " + identifier(old_table))
        cleanup.execute("SET FOREIGN_KEY_CHECKS=1")
    initialize_schema(destination, root)
    result = []
    with source.cursor() as src, destination.cursor() as dst:
        dst.execute('SHOW TABLES'); destination_tables = {r[0] for r in dst.fetchall()}
        # The destination has been checked unused, or belongs to this interrupted migration.
        dst.execute('SET FOREIGN_KEY_CHECKS=0')
        for table in destination_tables:
            if not table.startswith('ytx_'): raise RuntimeError('目标库发现非鱼太咸表，停止以防覆盖。')
            dst.execute('TRUNCATE TABLE ' + identifier(table))
        for entry in tables:
            source_table, target_table = entry['source'], entry['target']
            if entry['action'] == 'exclude':
                result.append(entry); continue
            if target_table not in destination_tables:
                raise RuntimeError(f'目标缺少表 {target_table}')
            src.execute('SHOW COLUMNS FROM '+identifier(source_table)); source_columns=[c[0] for c in src.fetchall()]
            dst.execute('SHOW COLUMNS FROM '+identifier(target_table)); target_columns=[c[0] for c in dst.fetchall()]
            dropped = set(source_columns) - set(target_columns)
            if dropped - set(mapping.get("excluded_columns", {}).get(source_table, [])):
                raise RuntimeError(f"{source_table} 存在未定义映射的字段：{sorted(dropped)}")
            columns=[c for c in source_columns if c in target_columns]
            sqlcolumns=','.join(identifier(c) for c in columns)
            src.execute('SELECT '+sqlcolumns+' FROM '+identifier(source_table)); raw=src.fetchall()
            expected=[v for row in raw if (v:=transform_row(source_table,columns,row,mapping,overrides)) is not None]
            if expected:
                sql='INSERT INTO '+identifier(target_table)+' ('+sqlcolumns+') VALUES ('+','.join(['%s']*len(columns))+')'
                for start in range(0,len(expected),200): dst.executemany(sql,expected[start:start+200])
            dst.execute('SELECT '+sqlcolumns+' FROM '+identifier(target_table)); actual=dst.fetchall()
            expected_hash,actual_hash=rows_digest(expected),rows_digest(actual)
            if len(actual)!=len(expected) or actual_hash!=expected_hash:
                raise RuntimeError(f'{target_table} 行数或内容校验失败。')
            src.execute('SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(source_table,))
            next_id=src.fetchone()[0]
            if next_id: dst.execute('ALTER TABLE '+identifier(target_table)+' AUTO_INCREMENT='+str(int(next_id)))
            if source_table.endswith('_settings'):
                key_index = columns.index('key')
                entry['excluded_setting_keys'] = sorted({row[key_index] for row in raw if filtered_setting(row[key_index],mapping)})
                entry['changed_setting_keys'] = sorted({row[key_index] for row in raw if source_table=='xy_system_settings' and row[key_index] in overrides})
            entry.update(imported=len(actual),excluded_rows=len(raw)-len(expected),sha256=actual_hash,
                         excluded_columns=sorted(set(source_columns)-set(columns)),verified=True)
            result.append(entry)
        dst.execute('SET FOREIGN_KEY_CHECKS=1')
    destination.commit()
    return result


def relationships(connection, prefix):
    results={}
    with connection.cursor() as cur:
        cur.execute('SHOW TABLES'); tables={x[0] for x in cur.fetchall()}
        for table in sorted(tables):
            if not table.startswith(prefix):continue
            cur.execute('SHOW COLUMNS FROM '+identifier(table));columns={c[0] for c in cur.fetchall()}
            for col in ('owner_id','user_id'):
                if col not in columns: continue
                if col == 'user_id' and table[len(prefix):] in {'token_cache','default_reply_records'}: continue  # Platform buyer/account identifiers, not application users.
                cur.execute('SELECT COUNT(*) FROM '+identifier(table)+' c LEFT JOIN '+identifier(prefix+'users')+' u ON c.'+identifier(col)+'=u.id WHERE c.'+identifier(col)+' IS NOT NULL AND c.'+identifier(col)+'<>0 AND u.id IS NULL')
                results[table[len(prefix):]+'.'+col]=cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM '+identifier(prefix+'card_item_relations')+' r LEFT JOIN '+identifier(prefix+'cards')+' c ON r.card_id=c.id WHERE c.id IS NULL')
        results['card_item_relations.card_id']=cur.fetchone()[0]
    return results


def copy_resources(backup, root):
    report=[]
    for name in ('static','browser_data'):
        source=backup/name; destination=root/'yutaixian_data'/name
        if not source.exists():continue
        destination.mkdir(parents=True,exist_ok=True)
        for path in sorted(source.rglob('*')):
            if not path.is_file() or path.is_symlink() or any(p in FILE_SKIP for p in path.parts):continue
            relative=path.relative_to(source);target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,target);os.chmod(target,0o600)
            before,after=digest_file(path),digest_file(target)
            if before!=after:raise RuntimeError('上传文件或浏览器资料校验失败。')
            report.append({'area':name,'path':str(relative),'sha256':after})
    return report


def copy_backup_archives(source, backup, root):
    report=[]
    with source.cursor() as cursor:
        cursor.execute('SELECT id,file_name FROM xy_db_backup_log')
        for identity,name in cursor.fetchall():
            if not name:continue
            original=backup/'backups'/Path(name).name
            if not original.is_file():continue  # Already pruned historical backups stay unavailable.
            filename=f'ytx_archive_{identity}.sql.gz'
            target=root/'yutaixian_data/backups'/filename
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(original,target);target.chmod(0o600)
            sha=digest_file(original)
            if sha!=digest_file(target):raise RuntimeError('历史备份文件校验失败。')
            report.append({'area':'backups','path':filename,'sha256':sha,'legacy_archive':True})
    return report


def import_redis(source, config):
    report=[]
    databases=int(source.config_get('databases')['databases'])
    for number in range(databases):
        source.execute_command('SELECT',number)
        destination=redis_connection(config,db=number)
        try:
            # Only the new private instance is cleared during this migration.
            destination.flushdb()
            for key in source.scan_iter():
                try:target=redis_key(key)
                except UnicodeDecodeError: raise RuntimeError('Redis 包含非 UTF-8 键，需明确映射后迁移。')
                digest=hashlib.sha256(key).hexdigest()
                if target is None:
                    report.append({'db':number,'key_sha256':digest,'action':'exclude-volatile'});continue
                ttl=source.pttl(key)
                if ttl==-2:continue
                expires=int(time.time()*1000)+ttl if ttl>=0 else None
                value=source.dump(key)
                if value is None:continue
                remaining=max(0,expires-int(time.time()*1000)) if expires else 0
                if expires and remaining<=0:continue
                destination.restore(target,remaining,value,replace=True)
                if destination.dump(target)!=value:raise RuntimeError('Redis 内容校验失败。')
                report.append({'db':number,'key_sha256':digest,'target_key_sha256':hashlib.sha256(target).hexdigest(),'action':'retain','expires_at_ms':expires,'verified':True})
            destination.save()
        finally:destination.close()
    return report


def run(stack, source_root, *, dry_run=False):
    root,data,env,metadata,tools=source_info(source_root)
    if root==stack.root:raise RuntimeError('来源和目标目录不能相同。')
    mapping=load(stack.root/'config/migration-map.json')
    state_path=stack.runtime/'data-migration.json';state=load(state_path)
    if state and state.get('source')!=str(root):raise RuntimeError('已有另一来源的迁移记录，不能覆盖。')
    if state.get('phase') in ('complete','activated'):
        print('迁移已校验完成；为保护新数据，本次未重复导入。');return state
    if dry_run:
        with source_connection(env) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT @@server_uuid')
                if cur.fetchone()[0]!=metadata['server_uuid']:raise RuntimeError('来源端口不是预期 MySQL 实例。')
            report=inventory(conn,mapping)
        print(json.dumps({'dry_run':True,'tables':report,'excluded_setting_prefixes':mapping['excluded_setting_prefixes'],'source_tables':len(report)},ensure_ascii=False,indent=2))
        return report
    stack.stop()
    stack.prepare()
    config=read_config(stack.root)
    stack.preflight_ports(config,{})
    infra=NativeInfrastructure(stack,config)
    try:
        infra.start()
        with mysql_connection(config) as conn:verify_unused(conn,state)
    finally:stack.cleanup_children();stack.children=[]
    if not state.get('snapshot_complete'):
        result=subprocess.run(['bash',str(root/'dev.sh'),'stop'],cwd=root)
        if result.returncode:raise RuntimeError('原实例停止失败，未开始迁移。')
        assert_ports_closed(env)
        backup=stack.root.parent/'.yutaixian-backups'/('migration-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3))
        backup.mkdir(parents=True,mode=0o700)
        state={'source':str(root),'phase':'snapshot','backup':str(backup),'snapshot_complete':False,'started_at':datetime.now().isoformat(),
               'setting_overrides':{'security.jwt_secret_key':secrets.token_hex(32),'security.internal_api_token':credentials(config)['INTERNAL_API_TOKEN'],
                    'login.system_name':'鱼太咸','login.system_title':'鱼太咸','login.system_description':'让店铺管理更轻松',
                    'show_default_login_info':'false','disclaimer.title':'使用说明','disclaimer.content':'请妥善保管账号和业务数据，并遵守平台规则。'}}
        private_json(state_path,state)
        for name in ('native','static','browser_data','backups'):
            if (data/name).exists():shutil.copytree(data/name,backup/name,symlinks=True)
        shutil.copy2(root/'.env',backup/'source.env');os.chmod(backup/'source.env',0o600)
        private_json(backup/'source-tools.json',tools)
        private_json(backup/'source-metadata.json',metadata)
        state['snapshot_checksums'] = {str(p.relative_to(backup)): digest_file(p) for p in backup.rglob('*') if p.is_file() and not p.is_symlink()}
        state['snapshot_complete']=True;state['phase']='importing';private_json(state_path,state)
    backup=Path(state['backup'])
    if any(not (backup / name).is_file() or digest_file(backup / name) != checksum for name, checksum in state.get('snapshot_checksums', {}).items()):
        raise RuntimeError('停写快照校验失败，未继续导入。')
    tools=load(backup/'source-tools.json');metadata=load(backup/'source-metadata.json')
    snapshot_env=dotenv_values(backup/'source.env',interpolate=False)
    try:
        assert_ports_closed(env)
        infra=NativeInfrastructure(stack,config);infra.start()
        with snapshot_services(backup,tools,metadata,stack) as (source,source_redis), mysql_connection(config) as destination:
            source.select_db(snapshot_env.get('MYSQL_DATABASE','xianyu_data'))
            table_report=import_tables(source,destination,mapping,state,config,stack.root)
            before=relationships(source,'xy_');after=relationships(destination,'ytx_')
            kept={e['source'][3:] for e in table_report if e['action']=='retain' and e['source'].startswith('xy_')}
            before={k:v for k,v in before.items() if k.split('.')[0] in kept}
            if before!=after:raise RuntimeError('关联完整性校验不一致。')
            files=copy_resources(backup,stack.root)
            files+=copy_backup_archives(source,backup,stack.root)
            redis_report=import_redis(source_redis,config)
            report={'completed_at':datetime.now().isoformat(),'tables':table_report,'relationships':after,'files':files,'redis':redis_report,
                    'excluded_settings':mapping['excluded_setting_prefixes']+sorted(EXCLUDED_USER_KEYS),'regenerated_secrets':['database','redis','JWT','internal_service_token'],
                    'backup':str(backup),'source_untouched':True}
            private_json(stack.runtime/'migration-report.json',report)
            state.update(phase='complete',completed_at=report['completed_at'])
            private_json(state_path,state)
        print('迁移和逐表、文件、Redis 校验通过。报告：'+str(stack.runtime/'migration-report.json'))
        print('原始停写备份：'+str(backup))
        return state
    except BaseException:
        state['phase']='failed';private_json(state_path,state)
        raise
    finally:
        stack.cleanup_children();stack.children=[]
