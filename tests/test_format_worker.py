# -*- coding: utf-8 -*-
"""【阿里云版】海口经济学院排版交给海外排版服务器：协议、端到端、线路不通改走云端 API、选路。

用真实的 uvicorn 在本机起一个「排版服务器」，主站这一侧用 core/format_remote.py 去连它；
LibreOffice 排版本身用假的 _run_school 代替。全部离线。"""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import uvicorn
from fastapi.testclient import TestClient

from core import accounts, alerts, format_remote
from tests import signed_in
from web import app as web
from web.account_routes import current_user

META = {'title': '论文', 'authorName': '张三', 'ruleId': 'school:hkjjxy', 'sourceSuffix': '.docx'}


def fake_run(job, payload, meta, delay=0.1, fail=False):
    """代替 schoolformat.run：写出 docx / pdf，更新任务状态。"""
    time.sleep(delay)
    work = Path(job['tmp'])
    job['log'].append('排版 %s' % meta['title'])
    if fail:
        job['state'], job['stage'] = 'error', '原稿结构无法识别'
        return
    (work / 'out.docx').write_bytes(b'DOCX:' + payload[:16])
    (work / 'out.pdf').write_bytes(b'%PDF-fake')
    job.update(files={'docx': str(work / 'out.docx'), 'pdf': str(work / 'out.pdf')},
               suggestions=['补全封面'], stage='排版完成', state='done')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class WorkerServer:
    """在本机真起一个排版服务器（就是同一个 web.app，开着内部接口）。"""

    def __init__(self):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(web.app, host='127.0.0.1', port=self.port,
                                                    log_level='error', lifespan='off'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.port

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


class Base(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'FORMAT_WORKER_TOKEN': 'tok', 'FORMAT_WORKER_URL': 'http://127.0.0.1:1',
                                      'FORMAT_WORKER_TOKEN_FILE': ''})
        env.start(); self.addCleanup(env.stop)
        for p in (patch.object(web, 'LOCAL_FORMAT', True), patch.object(web, 'REMOTE_POLL', 0.05),
                  patch.object(web, '_run_school', fake_run), patch.object(format_remote, '_sleep', lambda s: None)):
            p.start(); self.addCleanup(p.stop)
        format_remote.reset()
        before = set(web.JOBS)

        def drop_new_jobs():                               # 连任务目录一起删，免得下次启动被 _restore_jobs 恢复出来
            for key in set(web.JOBS) - before:
                job = web.JOBS.pop(key, None)
                shutil.rmtree((job or {}).get('tmp', ''), ignore_errors=True)
        self.addCleanup(drop_new_jobs)
        self.servers = []
        self.addCleanup(lambda: [s.stop() for s in self.servers])

    def start_worker(self):
        server = WorkerServer()
        self.servers.append(server)
        env = patch.dict(os.environ, {'FORMAT_WORKER_URL': server.url})
        env.start(); self.addCleanup(env.stop)
        return server

    def new_job(self):
        job = web._new_job(kind='school')
        job.update(name='论文', school_key='school:hkjjxy', stage='准备', status=None, statusLabel='',
                   taskId=None, suggestions=[], preview=None, exported=False)
        return job

    def wait_done(self, remote, seconds=10):
        end = time.time() + seconds
        while time.time() < end:
            info = format_remote.status(remote)
            if info['state'] != 'running':
                return info
            time.sleep(0.05)
        self.fail('worker job did not finish')


class Protocol(Base):
    def test_feature_is_off_without_a_token_and_needs_the_right_one_with_it(self):
        client = TestClient(web.app)
        with patch.dict(os.environ, {'FORMAT_WORKER_TOKEN': ''}):
            self.assertEqual(client.get('/internal/format-worker/health').status_code, 404)
        self.assertEqual(client.get('/internal/format-worker/health').status_code, 401)
        self.assertEqual(client.get('/internal/format-worker/health', headers={'Authorization': 'Bearer nope'}).status_code, 401)
        ok = client.get('/internal/format-worker/health', headers={'Authorization': 'Bearer tok'})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()['ok'])

    def test_feature_is_off_on_a_machine_without_libreoffice(self):
        with patch.object(web, 'LOCAL_FORMAT', False):
            res = TestClient(web.app).get('/internal/format-worker/health', headers={'Authorization': 'Bearer tok'})
        self.assertEqual(res.status_code, 404)

    def test_upload_is_idempotent_so_a_retried_upload_is_not_formatted_twice(self):
        self.start_worker()
        first = format_remote.submit(b'PK-data', 'a.docx', META, key='j-1')
        second = format_remote.submit(b'PK-data', 'a.docx', META, key='j-1')
        other = format_remote.submit(b'PK-data', 'a.docx', META, key='j-2')
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_only_whitelisted_meta_reaches_the_formatter_and_the_rule_is_forced(self):
        self.start_worker()
        seen = {}

        def spy(job, payload, meta):
            seen.update(meta)
            fake_run(job, payload, meta, delay=0)
        evil = dict(META, ruleId='dayipaper:9', tmp='/etc', files={'x': 1}, tutorName='李老师',
                    overrides=[{'idx': 2, 'role': 'heading', 'level': 2}])
        with patch.object(web, '_run_school', spy):
            remote = format_remote.submit(b'PK', 'a.docx', evil, key='j-meta')
            self.wait_done(remote)
        self.assertEqual(seen['ruleId'], 'school:hkjjxy')
        self.assertEqual(seen['tutorName'], '李老师')
        self.assertEqual(seen['overrides'], [{'idx': 2, 'role': 'heading', 'level': 2}])
        self.assertNotIn('tmp', seen)
        self.assertNotIn('files', seen)

    def test_a_full_worker_says_busy(self):
        self.start_worker()
        with patch.object(web, 'MAX_RUNNING', 0):
            with self.assertRaises(format_remote.Busy):
                format_remote.submit(b'PK', 'a.docx', META, key='j-busy')

    def test_wrong_token_is_rejected_not_retried(self):
        self.start_worker()
        with patch.object(format_remote, '_bearer', lambda: 'wrong'):     # 只改主站这一侧的密钥
            with self.assertRaises(format_remote.Rejected) as caught:
                format_remote.submit(b'PK', 'a.docx', META, key='j-token')
        self.assertEqual(caught.exception.status, 401)

    def test_bad_input_is_rejected(self):
        self.start_worker()
        with self.assertRaises(format_remote.Rejected):
            format_remote.submit(b'', 'a.docx', META, key='j-empty')
        with self.assertRaises(format_remote.Rejected):
            format_remote.submit(b'PK', 'a.docx', dict(META, sourceSuffix='.exe'), key='j-ext')
        with self.assertRaises(format_remote.Rejected):
            format_remote.submit(b'PK', 'a.docx', {'title': ''}, key='j-title')

    def test_files_come_with_a_checksum_and_unknown_things_are_gone(self):
        self.start_worker()
        remote = format_remote.submit(b'PK-data-0123456789', 'a.docx', META, key='j-files')
        info = self.wait_done(remote)
        self.assertEqual(sorted(info['files']), ['docx', 'pdf'])
        with tempfile.TemporaryDirectory() as tmp:
            path = format_remote.fetch(remote, 'docx', Path(tmp) / 'r.docx')
            self.assertEqual(Path(path).read_bytes(), b'DOCX:PK-data-01234567')
            self.assertFalse((Path(tmp) / 'r.docx.part').exists())
            with self.assertRaises(format_remote.Gone):
                format_remote.fetch(remote, 'exe', Path(tmp) / 'x')
        with self.assertRaises(format_remote.Gone):
            format_remote.status('j' + '0' * 32)

    def test_a_corrupted_download_is_not_accepted(self):
        self.start_worker()
        remote = format_remote.submit(b'PK-data', 'a.docx', META, key='j-corrupt')
        self.wait_done(remote)
        real = format_remote._call

        def tampered(method, path, **kw):
            response = real(method, path, **kw)
            if '/files/' in path:
                response.headers.replace_header('X-Content-SHA256', '0' * 64)
            return response
        with tempfile.TemporaryDirectory() as tmp, patch.object(format_remote, '_call', tampered):
            with self.assertRaises(format_remote.Unreachable):
                format_remote.fetch(remote, 'docx', Path(tmp) / 'r.docx')
            self.assertFalse((Path(tmp) / 'r.docx').exists())

    def test_discard_frees_the_worker_copy(self):
        self.start_worker()
        remote = format_remote.submit(b'PK', 'a.docx', META, key='j-discard')
        self.wait_done(remote)
        format_remote.discard(remote)
        with self.assertRaises(format_remote.Gone):
            format_remote.status(remote)
        format_remote.discard(remote)                      # 再删一次也不报错


class HealthAndBreaker(Base):
    def test_health_is_cached_and_mark_down_flips_it_at_once_with_one_alert(self):
        self.start_worker()
        with patch.object(alerts, 'alert') as alert:
            self.assertTrue(format_remote.healthy())
            format_remote.mark_down('connection reset')
            self.assertFalse(format_remote.healthy())              # 缓存期内不再去问
            format_remote.mark_down('again')
            self.assertEqual(alert.call_count, 1)                  # 同一次掉线只报一次
            self.assertTrue(format_remote.healthy(force=True))     # 强制重查：它其实好了

    def test_unconfigured_or_unreachable_is_not_healthy(self):
        with patch.dict(os.environ, {'FORMAT_WORKER_URL': ''}):
            self.assertFalse(format_remote.configured())
            self.assertFalse(format_remote.healthy())
        with patch.object(alerts, 'alert') as alert:
            self.assertFalse(format_remote.healthy(force=True))    # http://127.0.0.1:1 没人监听
        self.assertEqual(alert.call_count, 1)

    def test_busy_flag_follows_the_workers_load(self):
        self.start_worker()
        with patch.object(web, 'MAX_RUNNING', 0):
            self.assertTrue(format_remote.healthy(force=True))
            self.assertTrue(format_remote.busy())
        self.assertTrue(format_remote.healthy(force=True))
        self.assertFalse(format_remote.busy())


class RemoteRunner(Base):
    def test_finished_job_gets_both_files_copied_home_and_the_worker_copy_deleted(self):
        self.start_worker()
        job = self.new_job()
        discarded = []
        real_discard = format_remote.discard
        with patch.object(format_remote, 'discard', lambda remote: (discarded.append(remote), real_discard(remote))):
            web._run_remote_school(job, b'PK-payload-0123456789', dict(META))
        self.assertEqual(job['state'], 'done', job['log'])
        self.assertEqual(Path(job['files']['docx']).read_bytes(), b'DOCX:PK-payload-01234')
        self.assertEqual(Path(job['files']['pdf']).read_bytes(), b'%PDF-fake')
        self.assertEqual(Path(job['files']['docx']).parent, Path(job['tmp']))   # 和本机排版一样落在任务目录
        self.assertTrue((Path(job['tmp']) / 'job.json').is_file())              # 重启后能恢复
        self.assertEqual(job['suggestions'], ['补全封面'])
        self.assertEqual(job['stage'], '排版完成')
        self.assertIn('排版 论文', job['log'])                                   # 对面的日志同步过来了
        self.assertEqual(len(discarded), 1)                                      # 拷完就让对面删掉
        with self.assertRaises(format_remote.Gone):
            format_remote.status(discarded[0])

    def test_formatting_error_is_reported_as_is_and_never_falls_back(self):
        self.start_worker()
        job = self.new_job()
        with patch.object(web, '_run_school', lambda j, p, m: fake_run(j, p, m, delay=0, fail=True)), \
             patch.object(web, '_run_cloud') as cloud:
            web._run_remote_school(job, b'PK', dict(META))
        self.assertEqual((job['state'], job['stage']), ('error', '原稿结构无法识别'))
        cloud.assert_not_called()                          # 原稿的问题，换云端也一样；不重复提交

    def test_unreachable_worker_falls_back_to_the_cloud_api_in_the_same_job(self):
        job = self.new_job()                               # FORMAT_WORKER_URL 指向没人监听的端口
        with patch.object(web, '_cloud_haikou_rule', return_value='7'), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(alerts, 'alert'):
            web._run_remote_school(job, b'PK-payload', dict(META, overrides=[{'idx': 1, 'role': 'body'}]))
        self.assertEqual(job['kind'], 'cloud')
        self.assertEqual(job['school_key'], 'school:hkjjxy')       # 优惠券照常能用
        run_job, payload, filename, meta, retag = cloud.call_args.args
        self.assertIs(run_job, job)
        self.assertEqual((payload, filename), (b'PK-payload', 'source.docx'))
        self.assertEqual(meta['ruleId'], '7')
        self.assertNotIn('sourceSuffix', meta)
        self.assertNotIn('overrides', meta)
        self.assertEqual(retag, [{'idx': 1, 'role': 'body'}])      # 确认过的标题级别照样带过去
        self.assertTrue(any('改用云端规范排版' in line for line in job['log']))
        self.assertFalse(format_remote.healthy())                  # 之后的新任务一开始就走云端

    def test_no_cloud_rule_means_a_clear_error_instead_of_a_stuck_job(self):
        job = self.new_job()
        with patch.object(web, '_cloud_haikou_rule', return_value=None), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(alerts, 'alert'):
            web._run_remote_school(job, b'PK', dict(META))
        self.assertEqual(job['state'], 'error')
        self.assertIn('暂时不可用', job['stage'])
        cloud.assert_not_called()

    def test_losing_the_worker_midway_also_falls_back(self):
        server = self.start_worker()
        job = self.new_job()
        slow = lambda j, p, m: fake_run(j, p, m, delay=30)           # 对面一直在排，直到我们把它停掉
        with patch.object(web, '_run_school', slow), patch.object(web, 'REMOTE_LOST_AFTER', 0.4), \
             patch.object(web, '_cloud_haikou_rule', return_value='7'), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(alerts, 'alert'):
            runner = threading.Thread(target=web._run_remote_school, args=(job, b'PK', dict(META)), daemon=True)
            runner.start()
            end = time.time() + 10
            while not any('已提交到排版服务器' in line for line in job['log']) and time.time() < end:
                time.sleep(0.05)
            server.stop()
            runner.join(15)
            self.assertFalse(runner.is_alive())
        cloud.assert_called_once()
        self.assertEqual(job['kind'], 'cloud')

    def test_a_worker_that_lost_the_job_on_restart_falls_back(self):
        self.start_worker()
        job = self.new_job()
        slow = lambda j, p, m: fake_run(j, p, m, delay=30)
        real_status = format_remote.status

        def vanished(remote):                                # 对面重启：任务没了，404
            raise format_remote.Gone('排版服务器上没有这个任务')
        with patch.object(web, '_run_school', slow), patch.object(format_remote, 'status', vanished), \
             patch.object(web, '_cloud_haikou_rule', return_value='7'), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(alerts, 'alert'):
            web._run_remote_school(job, b'PK', dict(META))
        cloud.assert_called_once()

    def test_a_busy_worker_is_not_a_reason_to_switch_to_the_cloud(self):
        self.start_worker()
        job = self.new_job()
        with patch.object(web, 'MAX_RUNNING', 0), patch.object(web, '_run_cloud') as cloud:
            web._run_remote_school(job, b'PK', dict(META))
        self.assertEqual(job['state'], 'error')
        self.assertIn('任务较多', job['stage'])
        cloud.assert_not_called()

    def test_a_wrong_token_is_treated_as_the_worker_being_down(self):
        self.start_worker()
        job = self.new_job()
        with patch.object(format_remote, '_bearer', lambda: 'wrong'), \
             patch.object(web, '_cloud_haikou_rule', return_value='7'), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(alerts, 'alert') as alert:
            web._run_remote_school(job, b'PK', dict(META))
        cloud.assert_called_once()
        alert.assert_called()                                # 密钥配错要让人知道


class Routing(Base):
    def test_route_priority(self):
        with patch.object(format_remote, 'configured', return_value=True), \
             patch.object(format_remote, 'healthy', return_value=True):
            self.assertEqual(web._school_route(), 'worker')
        with patch.object(format_remote, 'configured', return_value=True), \
             patch.object(format_remote, 'healthy', return_value=False):
            self.assertEqual(web._school_route(), 'local')            # 本机有 LibreOffice 就用本机
            with patch.object(web, 'LOCAL_FORMAT', False):
                self.assertEqual(web._school_route(), 'cloud')        # 都没有才走云端 API
        with patch.dict(os.environ, {'FORMAT_WORKER_URL': ''}):
            self.assertEqual(web._school_route(), 'local')
            with patch.object(web, 'LOCAL_FORMAT', False):
                self.assertFalse(web._school_available())

    def test_cloud_rule_lookup_is_cached_and_survives_a_failing_search(self):
        web._HAIKOU_CLOUD.update(at=0.0, id=None)
        items = {'items': [{'id': 8, 'name': '海南大学'}, {'id': 7, 'name': '海口经济学院本科论文'}]}
        with patch.object(web.dayipaper, 'search_rules', return_value=items) as search:
            self.assertEqual(web._cloud_haikou_rule(), '7')
            self.assertEqual(web._cloud_haikou_rule(), '7')
            self.assertEqual(search.call_count, 1)
        web._HAIKOU_CLOUD['at'] = 0.0                                    # 缓存过期后搜索又失败：沿用旧值
        with patch.object(web.dayipaper, 'search_rules', side_effect=web.dayipaper.ApiError('down')):
            self.assertEqual(web._cloud_haikou_rule(), '7')
        web._HAIKOU_CLOUD.update(at=0.0, id=None)


class SubmitEndpoint(Base):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = patch.object(accounts, '_db_path', lambda: Path(self.temp.name) / 'accounts.sqlite3')
        db.start(); self.addCleanup(db.stop)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db'),
                                      'FORMAT_WORKER_URL': 'http://worker.invalid'})
        env.start(); self.addCleanup(env.stop)
        self.addCleanup(web.app.dependency_overrides.__setitem__, current_user, signed_in)
        web.app.dependency_overrides[current_user] = lambda: {'id': 21, 'username': 'u'}
        self.client = TestClient(web.app)

    def submit(self):
        return self.client.post('/api/format', data={'ruleId': 'school:hkjjxy', 'title': '论文', 'authorName': '张三'},
                                files={'file': ('a.docx', b'PK-payload')})

    def test_healthy_worker_gets_the_job_and_nothing_runs_locally(self):
        with patch.object(web, 'LOCAL_FORMAT', False), patch.object(format_remote, 'healthy', return_value=True), \
             patch.object(web, '_run_remote_school') as remote, patch.object(web, '_run_school') as local:
            res = self.submit()
        self.assertEqual(res.status_code, 200, res.text)
        remote.assert_called_once()
        local.assert_not_called()
        job, payload, meta = remote.call_args.args
        self.assertEqual((payload, meta['sourceSuffix'], meta['ruleId']), (b'PK-payload', '.docx', 'school:hkjjxy'))
        self.assertEqual(job['kind'], 'school')

    def test_down_worker_sends_a_new_job_straight_to_the_cloud_api(self):
        with patch.object(web, 'LOCAL_FORMAT', False), patch.object(format_remote, 'healthy', return_value=False), \
             patch.object(web, '_cloud_haikou_rule', return_value='7'), \
             patch.object(web.dayipaper, 'has_token', return_value=True), \
             patch.object(web, '_run_cloud') as cloud, patch.object(web, '_run_remote_school') as remote:
            res = self.submit()
        self.assertEqual(res.status_code, 200, res.text)
        remote.assert_not_called()
        job, payload, filename, meta = cloud.call_args.args[:4]
        self.assertEqual(meta['ruleId'], '7')
        self.assertEqual(job['kind'], 'cloud')
        self.assertEqual(job['school_key'], 'school:hkjjxy')
        self.assertTrue(any('已改用云端' in line for line in job['log']))

    def test_down_worker_and_no_cloud_rule_is_a_503(self):
        with patch.object(web, 'LOCAL_FORMAT', False), patch.object(format_remote, 'healthy', return_value=False), \
             patch.object(web, '_cloud_haikou_rule', return_value=None), \
             patch.object(web.dayipaper, 'has_token', return_value=True):
            self.assertEqual(self.submit().status_code, 503)

    def test_busy_worker_is_a_503_asking_to_retry_later(self):
        with patch.object(web, 'LOCAL_FORMAT', False), patch.object(format_remote, 'healthy', return_value=True), \
             patch.object(format_remote, 'busy', return_value=True), patch.object(web, '_run_remote_school') as remote:
            res = self.submit()
        self.assertEqual(res.status_code, 503)
        self.assertIn('较多', res.json()['detail'])
        remote.assert_not_called()

    def test_without_any_local_engine_or_worker_it_is_still_a_400_like_before(self):
        with patch.object(web, 'LOCAL_FORMAT', False), patch.dict(os.environ, {'FORMAT_WORKER_URL': ''}):
            self.assertEqual(self.submit().status_code, 400)

    def test_school_stays_searchable_when_only_the_worker_has_libreoffice(self):
        with patch.object(web, 'LOCAL_FORMAT', False):
            items = self.client.get('/api/format/rules', params={'q': '海口经济学院'}).json()['items']
        self.assertEqual([r['id'] for r in items], ['school:hkjjxy'])


if __name__ == '__main__':
    unittest.main()
