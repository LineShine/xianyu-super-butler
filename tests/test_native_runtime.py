"""Real native database round-trip, migration and Redis TTL tests; all fixtures private."""
import json,os,shutil,socket,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from local_stack import LocalStack
from native_config import read_config
from native_infra import NativeInfrastructure,mysql_connection,redis_connection,check_native
from data_migration import initialize_schema,import_tables,rows_digest,redis_key,import_redis,verify_unused

class MigrationUnitTests(unittest.TestCase):
    def test_payload_is_not_globally_renamed(self):
        from data_migration import transform_row
        mapping=json.loads((ROOT/'config/migration-map.json').read_text())
        row=(7,'xy_cards, xianyu-auto-reply, ${card}: 不应改变')
        self.assertEqual(transform_row('xy_cards',['id','data_content'],row,mapping,{}),row)
    def test_expired_locks_are_not_imported(self):
        self.assertIsNone(redis_key(b'lock:delivery:123'))
        self.assertIsNone(redis_key(b'ytx:session:abc'))
        self.assertEqual(redis_key(b'chat:avatar:abc'),b'ytx:chat:avatar:abc')
        self.assertEqual(redis_key(b'ytx:chat:avatar:abc'),b'ytx:chat:avatar:abc')
    def test_digest_preserves_duplicate_count_and_ignores_order(self):
        self.assertEqual(rows_digest([(1,'A'),(2,'B')]),rows_digest([(2,'B'),(1,'A')]))
        self.assertNotEqual(rows_digest([(1,'A')]),rows_digest([(1,'A'),(1,'A')]))

@unittest.skipUnless(os.environ.get('RUN_NATIVE_DB_TESTS')=='1','Opt in to private native DB fixtures')
class NativeDatabaseTests(unittest.TestCase):
    def make_stack(self):
        temp=tempfile.TemporaryDirectory(prefix='ytx-native-');self.addCleanup(temp.cleanup)
        root=Path(temp.name);shutil.copytree(ROOT/'config',root/'config')
        ports=[]
        for _ in range(2):
            with socket.socket() as listener:listener.bind(('127.0.0.1',0));ports.append(listener.getsockname()[1])
        (root/'.env').write_text(f'MYSQL_DATABASE=fixture\nMYSQL_USER=fixture\nMYSQL_PASSWORD=test-secret\nREDIS_PASSWORD=redis-secret\nINTERNAL_API_TOKEN=test-token\nLOCAL_MYSQL_PORT={ports[0]}\nLOCAL_REDIS_PORT={ports[1]}\n')
        stack=LocalStack(root);self.addCleanup(stack.cleanup_children)
        config=read_config(root);infra=NativeInfrastructure(stack,config);infra.prepare_tools();infra.start();check_native(config)
        return stack,config,infra
    def test_schema_migration_content_restart_and_ttl(self):
        source,sc,si=self.make_stack();destination,dc,di=self.make_stack()
        mapping=json.loads((ROOT/'config/migration-map.json').read_text())
        with mysql_connection(sc) as src, mysql_connection(dc) as dst:
            initialize_schema(src,ROOT)
            with src.cursor() as c:
                c.execute('SHOW TABLES')
                for (table,) in c.fetchall():c.execute('RENAME TABLE `'+table+'` TO `xy_'+table[4:]+'`')
                c.execute("INSERT INTO xy_users (id,username,email,password_hash,role,status) VALUES (1,'tester','test@example.invalid','hash','admin','active')")
                c.execute('INSERT INTO xy_cards (id,user_id,name,type,data_content,is_multi_spec,spec_name,spec_value) VALUES (7,1,%s,%s,%s,1,%s,%s)',('卡券','data','xy_cards\n卡密不得替换 $\\\x00','类型','大份'))
                c.execute("ALTER TABLE xy_card_item_relations ADD COLUMN dock_record_id BIGINT NOT NULL DEFAULT 0")
                c.execute("INSERT INTO xy_card_item_relations (user_id,card_id,item_id,source,dock_record_id) VALUES (1,7,'item','own',0)")
                c.execute("INSERT INTO xy_system_settings (`key`,value) VALUES ('security.jwt_secret_key','old-secret'),('auth.footer_ad_html','remove')")
                c.execute('CREATE TABLE xy_announcements (id INT PRIMARY KEY,content TEXT)')
                c.execute("INSERT INTO xy_announcements VALUES (1,'remove')")
            src.commit()
            verify_unused(dst,{})
            report=import_tables(src,dst,mapping,{'setting_overrides':{'security.jwt_secret_key':'new-secret'}},dc,ROOT)
            self.assertTrue(all(r.get('verified') for r in report if r['action']=='retain'))
            with dst.cursor() as c:
                c.execute('SHOW COLUMNS FROM ytx_card_item_relations');self.assertNotIn('dock_record_id',[v[0] for v in c.fetchall()])
                c.execute('SHOW COLUMNS FROM ytx_cards');self.assertNotIn('is_dockable',[v[0] for v in c.fetchall()])
                c.execute('SELECT data_content FROM ytx_cards');self.assertEqual(c.fetchone()[0],'xy_cards\n卡密不得替换 $\\\x00')
                c.execute('SELECT value FROM ytx_system_settings');self.assertEqual(c.fetchall(),(('new-secret',),))
            with self.assertRaisesRegex(RuntimeError,'业务数据'):verify_unused(dst,{})
            # A failed pre-activation import can be resumed from the same snapshot.
            with dst.cursor() as c:c.execute('DELETE FROM ytx_users')
            dst.commit()
            verify_unused(dst,{'snapshot_complete':True,'phase':'failed'})
            resumed=import_tables(src,dst,mapping,{'setting_overrides':{'security.jwt_secret_key':'new-secret'}},dc,ROOT)
            self.assertTrue(all(r.get('verified') for r in resumed if r['action']=='retain'))
            with dst.cursor() as c:c.execute('SELECT COUNT(*) FROM ytx_users');self.assertEqual(c.fetchone()[0],1)

        with redis_connection(sc) as src:
            src.set('persistent','value');src.hset('hash',mapping={'field':'value'});src.set('expiry','value',px=20000);src.set('lock:delivery:order','locked')
            report=import_redis(src,dc)
        with redis_connection(dc) as dst:
            self.assertEqual(dst.get('ytx:persistent'),b'value');self.assertEqual(dst.hget('ytx:hash','field'),b'value')
            self.assertGreater(dst.pttl('ytx:expiry'),0);self.assertLessEqual(dst.pttl('ytx:expiry'),20000)
            self.assertFalse(dst.exists('ytx:lock:delivery:order'))
        # Exercise actual SQL row locks and the shared Redis order lock concurrently.
        import asyncio
        from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
        from sqlalchemy.engine import URL
        from sqlalchemy import text
        from common.services.card_delivery_content import consume_batch_data
        from common.db import redis_client as locks
        import redis.asyncio as async_redis
        from unittest.mock import patch,AsyncMock
        async def shipping_check():
            engine=create_async_engine(URL.create('mysql+asyncmy',username='fixture',password='test-secret',host='127.0.0.1',port=int(dc['services']['mysql']['ports'][0]['published']),database='fixture'))
            sessions=async_sessionmaker(engine,expire_on_commit=False)
            from common.services.card_matcher import CardMatcher
            async with sessions.begin() as session:
                matcher=CardMatcher(session)
                matched=await matcher.get_cards_by_item_id('item','类型','大份')
                self.assertEqual([c['id'] for c in matched],[7])
                await matcher.update_item_card_relations('item',1,[{'card_id':7,'source':'own'}])
                linked=await matcher.get_all_cards_by_item_id('item')
                self.assertEqual([c['id'] for c in linked],[7])
            async with sessions.begin() as session:
                await session.execute(text("UPDATE ytx_cards SET data_content=:content WHERE id=7"),{'content':'TEST-1\nTEST-2\nTEST-3'})
            async def take():
                async with sessions.begin() as session:return await consume_batch_data(session,7)
            sent=await asyncio.gather(take(),take(),take())
            self.assertEqual(set(sent),{'TEST-1','TEST-2','TEST-3'})
            self.assertIsNone(await take())
            client=async_redis.Redis(host='127.0.0.1',port=int(dc['services']['redis']['ports'][0]['published']),password='redis-secret')
            with patch.object(locks,'get_redis_client',new=AsyncMock(return_value=client)):
                realtime=locks.DistributedLock('same-order');scheduled=locks.DistributedLock('same-order')
                attempts=await asyncio.gather(realtime.acquire(blocking=False),scheduled.acquire(blocking=False))
                self.assertEqual(sorted(attempts),[False,True])
                await (realtime if attempts[0] else scheduled).release()
            await client.aclose();await engine.dispose()
        asyncio.run(shipping_check())
        destination.cleanup_children();destination.children=[];di=NativeInfrastructure(destination,dc);di.start();check_native(dc)
        with mysql_connection(dc) as dst:
            with dst.cursor() as c:c.execute('SELECT COUNT(*) FROM ytx_cards');self.assertEqual(c.fetchone()[0],1)
        with redis_connection(dc) as dst:self.assertEqual(dst.get('ytx:persistent'),b'value')
