# -*- coding: utf-8 -*-
"""排版开放接口的离线端到端测试。

用本地假服务模拟六个接口（含「鉴权失败返回 HTTP 401 + 正常信封」这个真实行为），
所以不联网、不需要 PAT、也不产生任何计费：

    python -m unittest discover -s tests -t .      # 或 python -m unittest tests.test_dayipaper
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 临时目录收进工作区：Web 层的 job 临时目录走 tempfile，落到系统 Temp 可能没有写权限
_ORIGINAL_TEMPDIR = tempfile.tempdir
_TEST_PARENT = ROOT / '.test-tmp'
_TEST_PARENT.mkdir(exist_ok=True)
_TMP_ROOT = Path(tempfile.mkdtemp(prefix='offline-', dir=_TEST_PARENT))
tempfile.tempdir = str(_TMP_ROOT)

from core import dayipaper                                    # noqa: E402

DOCX = b"PK\x03\x04" + b"fake-docx-payload" * 8
PAT = "test-pat"
TASK = "6a6aa9726ecd942cb04446a9"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeDayipaper"

    def log_message(self, *args):        # 测试里别刷屏
        pass

    # ---- 底层收发 ----
    def _send(self, status, payload, ctype="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _env(self, data=None, code=0, message=None, status=200):
        self._send(status, {"code": code, "message": message, "data": data})

    def _authed(self):
        store = self.server.store
        return self.headers.get("Authorization") == "Bearer " + store["pat"]

    def _query(self):
        return dict(urllib.parse.parse_qsl(self.path.partition("?")[2]))

    # ---- 路由 ----
    def do_GET(self):
        store = self.server.store
        path = self.path.partition("?")[0]
        query = self._query()

        # 预签名地址：真实环境是 OSS，不带任何鉴权头
        if path == "/oss/docx":
            store["downloads"] += 1
            return self._send(200, store["docx"], "application/octet-stream")
        if path == "/oss/preview.pdf":
            return self._send(200, b"%PDF-1.4 fake", "application/pdf")

        if not self._authed():
            return self._env(None, 401, "登录状态无效，请先登录", 401)

        if path.endswith("/searchRule"):
            store["searches"] += 1
            return self._env(store["rules"])

        if path.endswith("/traceTask"):
            index = min(store["polls"], len(store["statuses"]) - 1)
            store["polls"] += 1
            status = store["statuses"][index]
            info = {"id": query.get("taskId"), "status": status,
                    "statusLabel": "状态-" + status, "ruleName": "xx学院本科论文格式规范"}
            if status == "COMPLETED":
                info["pdfShareUrl"] = self.server.base + "/oss/preview.pdf"
            if status == "ERROR":
                info["errorReason"] = "源文档无法打开"
            return self._env(info)

        if path.endswith("/formatSuggestions"):
            return self._env(store["suggestions"])

        if path.endswith("/downloadFormattedDocument"):
            store["exports"] += 1
            store["export_fmt"] = query.get("format")
            return self._env(self.server.base + "/oss/docx?format=%s" % query.get("format"))

        return self._env(None, 404, "未知接口", 404)

    def do_POST(self):
        store = self.server.store
        path = self.path.partition("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if not self._authed():
            return self._env(None, 401, "登录状态无效，请先登录", 401)

        if path.endswith("/uploadRowDocument"):
            store["upload_hits"] += 1
            store["upload_body"] = body
            store["upload_ctype"] = self.headers.get("Content-Type") or ""
            return self._env("document/format/tmp/2026-07-30/abc123.docx")

        if path.endswith("/requestDocumentFormat"):
            store["format_request"] = json.loads(body.decode("utf-8"))
            return self._env(TASK)

        return self._env(None, 404, "未知接口", 404)


class FakeApi:
    """本地假服务 + 环境变量接管，退出时全部还原。"""

    def __init__(self, statuses=None, rules=None, suggestions=None, pat=PAT, docx=DOCX):
        self.store = {
            "pat": pat, "statuses": statuses or ["CREATED", "FORMATTING", "COMPLETED"],
            "polls": 0, "searches": 0, "exports": 0, "downloads": 0,
            "upload_hits": 0, "export_fmt": None,
            "rules": rules if rules is not None else {
                "pageNum": 1, "pageSize": 5, "total": 1,
                "items": [{"id": "rule-001", "name": "xx学院本科论文格式规范-文科类(2026)",
                           "targetSchoolName": "xx学院", "brief": "适用于文科类本科毕业论文。"}]},
            "suggestions": suggestions if suggestions is not None else [
                {"message": "无法确定 \"1.信用惩戒的形式识别\" 是否为正文章节",
                 "rowText": "1.信用惩戒的形式识别", "warnLevel": 1}],
            "docx": docx,
        }
        self.saved = {}

    def __enter__(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
        httpd.store = self.store
        httpd.base = "http://127.0.0.1:%d" % httpd.server_address[1]
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()

        for key, value in (("DAYIPAPER_BASE", httpd.base), ("DAYIPAPER_PAT", self.store["pat"]),
                           ("NO_PROXY", "127.0.0.1,localhost"), ("no_proxy", "127.0.0.1,localhost")):
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        self.store["base"] = httpd.base
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("DAYIPAPER_BASE", "DAYIPAPER_PAT")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestClientLayer(Base):
    def test_full_flow_exports_docx(self):
        with FakeApi() as api:
            out_dir = _TMP_ROOT / ("out-" + uuid.uuid4().hex[:8])
            out_dir.mkdir(parents=True, exist_ok=True)
            result = dayipaper.format_document(
                DOCX, rule_id="rule-001", title="中文标题", author_name="周良",
                out_dir=str(out_dir), fmt=1, interval=1, timeout=30,
                extra={"tutorName": "李老师", "subjectName": "会计学 2027届"})

            self.assertEqual(result["taskId"], TASK)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(len(result["suggestions"]), 1)
            self.assertEqual(Path(result["file"]).read_bytes(), DOCX)

            # 上传：multipart、带边界、body 里含文件内容
            self.assertEqual(api.store["upload_hits"], 1)
            self.assertIn("multipart/form-data; boundary=", api.store["upload_ctype"])
            self.assertIn(b"fake-docx-payload", api.store["upload_body"])
            self.assertIn('name="file"', api.store["upload_body"].decode("utf-8", "replace"))

            # 发起排版：fileUri / ruleId / 必填项 / 选填项都带上了
            sent = api.store["format_request"]
            self.assertEqual(sent["fileUri"], "document/format/tmp/2026-07-30/abc123.docx")
            self.assertEqual(sent["ruleId"], "rule-001")
            self.assertEqual(sent["title"], "中文标题")
            self.assertEqual(sent["authorName"], "周良")
            self.assertEqual(sent["tutorName"], "李老师")
            self.assertEqual(sent["subjectName"], "会计学 2027届")

            # 导出：format=1（docx），地址只取了一次，成品落在指定目录
            self.assertEqual(api.store["export_fmt"], "1")
            self.assertEqual(api.store["exports"], 1)
            self.assertEqual(Path(result["file"]).parent, out_dir)

    def test_no_export_stops_before_billing(self):
        with FakeApi() as api:
            result = dayipaper.format_document(DOCX, rule_id="r", title="题", author_name="名",
                                               out_dir=str(_TMP_ROOT), export=False,
                                               interval=1, timeout=30)
            self.assertIsNone(result["file"])
            self.assertEqual(api.store["exports"], 0)          # 没导出 = 没计费
            self.assertIn("/oss/preview.pdf", result["previewUrl"])

    def test_poll_interval_is_floored_to_one_second(self):
        with FakeApi(statuses=["CREATED", "COMPLETED"]) as api:
            started = time.time()
            dayipaper.wait(TASK, interval=0.05, timeout=30)     # 0.05 应被夹到 1 秒
            elapsed = time.time() - started
            self.assertGreaterEqual(elapsed, 0.95)
            self.assertEqual(api.store["polls"], 2)

    def test_search_rule_and_pick(self):
        with FakeApi() as api:
            found = dayipaper.search_rules("xx学院")
            self.assertEqual(found["items"][0]["id"], "rule-001")
            picked = dayipaper.pick_rule("xx学院")
            self.assertEqual(picked["id"], "rule-001")
            self.assertEqual(api.store["searches"], 2)

    def test_http_401_envelope_becomes_apierror(self):
        """真实行为：PAT 无效时对方返回 HTTP 401 + 正常信封，不能当网络故障。"""
        with FakeApi(pat="right-pat"):
            os.environ["DAYIPAPER_PAT"] = "wrong-pat"
            with self.assertRaises(dayipaper.ApiError) as ctx:
                dayipaper.search_rules("xx学院")
            self.assertEqual(ctx.exception.http_status, 401)
            self.assertEqual(ctx.exception.code, 401)
            self.assertIn("登录状态无效", ctx.exception.message)

    def test_business_error_code_on_http_200(self):
        """HTTP 200 但信封 code != 0：以信封的 code/message 为准，不看 HTTP 状态。"""
        raw = json.dumps({"code": 7, "message": "参数错误", "data": None}).encode("utf-8")
        with self.assertRaises(dayipaper.ApiError) as ctx:
            dayipaper._unwrap(raw, 200)
        self.assertEqual(ctx.exception.code, 7)
        self.assertEqual(ctx.exception.message, "参数错误")

        # 非信封内容（网关错误页）也要给可读报错，而不是 JSON 解析异常
        with self.assertRaises(dayipaper.ApiError) as ctx:
            dayipaper._unwrap(b"<html>502 Bad Gateway</html>", 502)
        self.assertIn("非预期内容", ctx.exception.message)

    def test_error_status_raises_with_reason(self):
        with FakeApi(statuses=["FORMATTING", "ERROR"]):
            with self.assertRaises(dayipaper.ApiError) as ctx:
                dayipaper.wait(TASK, interval=1, timeout=30)
            self.assertIn("源文档无法打开", ctx.exception.message)

    def test_timeout_raises(self):
        with FakeApi(statuses=["FORMATTING"]):
            with self.assertRaises(dayipaper.ApiError) as ctx:
                dayipaper.wait(TASK, interval=1, timeout=1.5)
            self.assertIn("超时", ctx.exception.message)

    def test_missing_pat_is_a_readable_error(self):
        os.environ.pop("DAYIPAPER_PAT", None)
        self.assertFalse(dayipaper.has_token())
        with self.assertRaises(dayipaper.ApiError) as ctx:
            dayipaper.search_rules("x")
        self.assertIn("DAYIPAPER_PAT", ctx.exception.message)

    def test_unreachable_host_hint(self):
        os.environ["DAYIPAPER_BASE"] = "http://127.0.0.1:9"
        os.environ["DAYIPAPER_PAT"] = PAT
        with self.assertRaises(dayipaper.ApiError) as ctx:
            dayipaper.search_rules("x")
        self.assertIn("连不上", ctx.exception.message)

    def test_multipart_filename_is_escaped(self):
        with FakeApi() as api:
            dayipaper.upload_document(DOCX, 'a"b\r\nc.docx')
            body = api.store["upload_body"].decode("utf-8", "replace")
            self.assertIn('filename="abc.docx"', body)
            self.assertNotIn("\r\n\r\nc.docx", body)


class TestWebLayer(Base):
    """走 FastAPI 路由，验证串联逻辑（不联网，全部对本地假服务）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("THESIS_CLOUD_POLL", "1")
        from fastapi.testclient import TestClient
        from web import app as webapp
        cls.webapp = webapp
        # web/app.py 用 tempfile.mkdtemp 建任务目录。本机沙箱给 mkdtemp 建出的目录
        # 没有写权限（Path.mkdir 建的正常），所以测试里换成等价的实现；
        # 生产环境照常用 mkdtemp，这里不改变被测代码的行为。
        cls._real_mkdtemp = tempfile.mkdtemp

        def _mkdtemp(*args, **kwargs):
            path = _TMP_ROOT / ("job-" + uuid.uuid4().hex[:8])
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        tempfile.mkdtemp = _mkdtemp
        cls.client = TestClient(webapp.app)

    @classmethod
    def tearDownClass(cls):
        tempfile.mkdtemp = cls._real_mkdtemp

    def test_info_reports_channel_state(self):
        with FakeApi():
            body = self.client.get("/api/dayipaper").json()
            self.assertTrue(body["enabled"])
            self.assertGreaterEqual(body["poll"], 1)
        os.environ.pop("DAYIPAPER_PAT", None)
        self.assertFalse(self.client.get("/api/dayipaper").json()["enabled"])

    def test_format_rejected_without_pat(self):
        os.environ.pop("DAYIPAPER_PAT", None)
        resp = self.client.post("/api/dayipaper/format",
                                files={"file": ("论文.docx", DOCX, "application/octet-stream")},
                                data={"ruleId": "r", "title": "题", "authorName": "名"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("DAYIPAPER_PAT", resp.json()["detail"])

    def test_format_rejects_non_word_file(self):
        with FakeApi():
            resp = self.client.post("/api/dayipaper/format",
                                    files={"file": ("论文.pdf", b"%PDF", "application/pdf")},
                                    data={"ruleId": "r", "title": "题", "authorName": "名"})
            self.assertEqual(resp.status_code, 400)

    def test_full_flow_through_routes(self):
        with FakeApi(statuses=["CREATED", "FORMATTING", "COMPLETED"]) as api:
            resp = self.client.post(
                "/api/dayipaper/format",
                files={"file": ("论文.docx", DOCX, "application/octet-stream")},
                data={"ruleId": "rule-001", "title": "中文标题", "authorName": "周良",
                      "tutorName": "李老师"})
            self.assertEqual(resp.status_code, 200)
            job_id = resp.json()["job"]

            job = {}
            for _ in range(120):
                job = self.client.get("/api/dayipaper/job/" + job_id).json()
                if job["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(job["state"], "done", job)
            self.assertEqual(job["taskId"], TASK)
            self.assertEqual(len(job["suggestions"]), 1)
            self.assertTrue(job["preview"])

            # 预览是 303 跳转（原地址 3 分钟失效，每次现取）
            resp = self.client.get("/api/dayipaper/preview/" + job_id, follow_redirects=False)
            self.assertEqual(resp.status_code, 303)
            self.assertIn("/oss/preview.pdf", resp.headers["location"])

            # Word export requires an enabled, paid Alipay order.
            with patch.object(self.webapp.alipay, "public_status", return_value={"enabled": False}):
                resp = self.client.get("/api/dayipaper/download/" + job_id)
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(api.store["exports"], 0)

            # A paid order unlocks export, and the generated file remains cached.
            with patch.object(self.webapp.alipay, "public_status", return_value={"enabled": True}), patch.object(
                    self.webapp.alipay, "get_order", return_value={"status": "TRADE_SUCCESS"}):
                resp = self.client.get("/api/dayipaper/download/%s?confirm=true&payment_order=test-order" % job_id)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.content, DOCX)
            self.assertEqual(api.store["exports"], 1)

            with patch.object(self.webapp.alipay, "public_status", return_value={"enabled": True}), patch.object(
                    self.webapp.alipay, "get_order", return_value={"status": "TRADE_SUCCESS"}):
                resp = self.client.get("/api/dayipaper/download/%s?confirm=true&payment_order=test-order" % job_id)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(api.store["exports"], 1)
            self.assertEqual(self.client.get("/api/dayipaper/job/" + job_id).json()["exported"], True)

    def test_cloud_job_errors_are_reported_not_raised(self):
        with FakeApi(statuses=["FORMATTING", "ERROR"]):
            resp = self.client.post(
                "/api/dayipaper/format",
                files={"file": ("论文.docx", DOCX, "application/octet-stream")},
                data={"ruleId": "r", "title": "题", "authorName": "名"})
            job_id = resp.json()["job"]
            job = {}
            for _ in range(120):
                job = self.client.get("/api/dayipaper/job/" + job_id).json()
                if job["state"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(job["state"], "error")
            self.assertIn("源文档无法打开", "\n".join(job["log"]))

    def test_local_job_endpoint_ignores_cloud_jobs(self):
        with FakeApi():
            resp = self.client.post(
                "/api/dayipaper/format",
                files={"file": ("论文.docx", DOCX, "application/octet-stream")},
                data={"ruleId": "r", "title": "题", "authorName": "名"})
            job_id = resp.json()["job"]
            self.assertEqual(self.client.get("/api/job/" + job_id).status_code, 404)


def tearDownModule():
    """跑完清掉工作区里的临时目录。"""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    tempfile.tempdir = _ORIGINAL_TEMPDIR


if __name__ == "__main__":
    unittest.main(verbosity=2)
