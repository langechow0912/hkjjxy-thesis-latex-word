"""Regressions for "paid but cannot download" and the admin login loop."""
import base64
import json
import os
import tempfile
import unittest
import urllib.parse
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from core import alipay
import web.app as web


class PaidDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (self.root / "private.pem").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (self.root / "public.pem").write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.env = patch.dict(os.environ, {
            "ALIPAY_ENABLED": "true", "ALIPAY_APP_ID": "test-app",
            "ALIPAY_APP_PRIVATE_KEY_FILE": str(self.root / "private.pem"),
            "ALIPAY_PUBLIC_KEY_FILE": str(self.root / "public.pem"),
            "ALIPAY_NOTIFY_URL": "https://merchant.example/api/alipay/notify",
            "ALIPAY_RETURN_URL": "https://merchant.example/",
            "ALIPAY_SELLER_ID": "seller", "ALIPAY_MODE": "sandbox",
            "ALIPAY_ORDER_DB": str(self.root / "orders.sqlite3"),
            "THESIS_FORMAT_PRICE_CENTS": "2999",
        })
        self.env.start()
        alipay._LAST_QUERY.clear()
        self.job_id = "j" + "a" * 32
        work = self.root / "thesis-web-test"
        work.mkdir()
        (work / "formatted.docx").write_bytes(b"PK-docx")
        web.JOBS[self.job_id] = {"id": self.job_id, "kind": "school", "state": "done", "name": "论文",
                                 "school_key": "school:hkjjxy", "files": {"docx": str(work / "formatted.docx")},
                                 "tmp": str(work), "log": [], "t": 1}
        self.client = TestClient(web.app)

    def tearDown(self):
        web.JOBS.pop(self.job_id, None)
        self.env.stop()
        self.temp.cleanup()

    def query_response(self, order, status="TRADE_SUCCESS", amount=None):
        body = {"alipay_trade_query_response": {
            "code": "10000", "out_trade_no": order["out_trade_no"], "trade_no": "2026TRADE",
            "trade_status": status, "total_amount": amount or "29.99"}}
        return BytesIO(json.dumps(body).encode())

    def test_return_url_carries_job_and_order(self):
        order = alipay.create_order(self.job_id, "论文", resume_return=True)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(order["payment_url"]).query))
        returned = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(params["return_url"]).query))
        self.assertEqual(returned, {"job": self.job_id, "order": order["out_trade_no"]})

    def test_paid_order_without_notification_downloads_after_query(self):
        order = alipay.create_order(self.job_id, "论文")
        with patch("core.alipay.urllib.request.urlopen", return_value=self.query_response(order)):
            response = self.client.get(f"/api/format/jobs/{self.job_id}/download",
                                       params={"fmt": 1, "payment_order": order["out_trade_no"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"PK-docx")
        self.assertEqual(alipay.get_order(order["out_trade_no"])["status"], "TRADE_SUCCESS")

    def test_query_rejects_unpaid_and_wrong_amount(self):
        order = alipay.create_order(self.job_id, "论文")
        url = f"/api/format/jobs/{self.job_id}/payment/{order['out_trade_no']}"
        with patch("core.alipay.urllib.request.urlopen", return_value=self.query_response(order, "WAIT_BUYER_PAY")):
            self.assertFalse(self.client.get(url).json()["paid"])
        alipay._LAST_QUERY.clear()
        with patch("core.alipay.urllib.request.urlopen", return_value=self.query_response(order, amount="0.01")):
            self.assertFalse(self.client.get(url).json()["paid"])
        alipay._LAST_QUERY.clear()
        with patch("core.alipay.urllib.request.urlopen", return_value=self.query_response(order)):
            self.assertTrue(self.client.get(url).json()["paid"])

    def test_query_is_rate_limited(self):
        order = alipay.create_order(self.job_id, "论文")
        with patch("core.alipay.urllib.request.urlopen",
                   side_effect=lambda *a, **k: self.query_response(order, "WAIT_BUYER_PAY")) as gateway:
            for _ in range(3):
                alipay.sync_order(order["out_trade_no"], self.job_id)
        self.assertEqual(gateway.call_count, 1)

    def test_finished_job_survives_restart(self):
        job = web.JOBS.pop(self.job_id)
        web._save_job(job)
        with patch("web.app.tempfile.gettempdir", return_value=str(self.root)):
            web._restore_jobs()
        restored = web.JOBS[self.job_id]
        self.assertEqual(restored["files"]["docx"], job["files"]["docx"])
        self.assertEqual(restored["state"], "done")

    def test_paid_job_is_not_reaped(self):
        order = alipay.create_order(self.job_id, "论文")
        alipay._mark_paid(order["out_trade_no"], "T1")
        web.JOBS[self.job_id]["t"] = __import__("time").time()
        filler = {}
        for i in range(web.KEEP_JOBS + 2):
            jid = "jfill%d" % i
            filler[jid] = {"id": jid, "kind": "school", "state": "done", "t": web.JOBS[self.job_id]["t"] - 1, "tmp": ""}
        web.JOBS.update(filler)
        try:
            web._reap_jobs()
            self.assertIn(self.job_id, web.JOBS)
        finally:
            for jid in filler:
                web.JOBS.pop(jid, None)


class AlipayKeyFormatTests(PaidDownloadTests):
    def test_bare_base64_platform_key_verifies_notification(self):
        pem = (self.root / "public.pem").read_text()
        bare = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
        (self.root / "public.pem").write_text(bare)
        key = alipay._public_key(self.root / "public.pem")
        self.assertEqual(key.key_size, 2048)

    def test_return_url_defaults_to_site_home(self):
        with patch.dict(os.environ, {"ALIPAY_RETURN_URL": ""}):
            self.assertEqual(alipay.config()["return_url"], "https://merchant.example/")


class ServicePaymentTests(PaidDownloadTests):
    """API services (detection / rewriting) use the same Alipay confirmation."""

    def test_service_order_confirmed_by_query_without_notification(self):
        from core import service_billing
        quote = service_billing.quote("detect_text", {"text": "测试正文", "task_platform": "知网_zh",
                                                      "detection_type": "ai_rate", "language": "中文"})
        order = service_billing.create_payment(quote["quote_id"], resume_return=True)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(order["payment_url"]).query))
        self.assertIn("job=" + quote["quote_id"], params["return_url"])
        body = {"alipay_trade_query_response": {
            "code": "10000", "out_trade_no": order["out_trade_no"], "trade_no": "T2",
            "trade_status": "TRADE_SUCCESS", "total_amount": f"{order['amount_cents'] / 100:.2f}"}}
        with patch("core.alipay.urllib.request.urlopen", return_value=BytesIO(json.dumps(body).encode())):
            state = self.client.get(f"/api/tabword/billing/{quote['quote_id']}/{order['out_trade_no']}").json()
        self.assertEqual(state["status"], "TRADE_SUCCESS")
        self.assertEqual(state["service_state"], "ready")


class HaikouRoutingTests(unittest.TestCase):
    def test_short_school_names_use_local_template(self):
        client = TestClient(web.app)
        for query in ("海口经济学院", "海口经济", "海口", "hkjjxy"):
            items = client.get("/api/format/rules", params={"q": query}).json()["items"]
            self.assertEqual(items[0]["id"], "school:hkjjxy", query)

    def test_cloud_capacity_does_not_block_local_school(self):
        running = {"c%d" % i: {"id": "c%d" % i, "kind": "cloud", "state": "running"} for i in range(web.MAX_CLOUD)}
        web.JOBS.update(running)
        try:
            with patch.object(web.threading, "Thread"):
                response = TestClient(web.app).post(
                    "/api/format", data={"ruleId": "school:hkjjxy", "title": "t", "authorName": "a"},
                    files={"file": ("a.docx", b"PK")})
            self.assertEqual(response.status_code, 200)
            web.JOBS.pop(response.json()["job"], None)
        finally:
            for jid in running:
                web.JOBS.pop(jid, None)


class HaikouCloudResultTests(unittest.TestCase):
    def rules(self, query, items):
        with patch.object(web.dayipaper, "has_token", return_value=True), \
             patch.object(web.dayipaper, "search_rules", return_value={"total": len(items), "items": items}):
            return TestClient(web.app).get("/api/format/rules", params={"q": query}).json()["items"]

    def test_haikou_in_cloud_results_is_replaced_by_local(self):
        items = self.rules("海南", [{"id": 7, "name": "海口经济学院本科论文"}, {"id": 8, "name": "海南大学"}])
        self.assertEqual([r["id"] for r in items], ["school:hkjjxy", "dayipaper:8"])

    def test_switch_off_routes_haikou_to_cloud(self):
        with patch.object(web, "LOCAL_FORMAT", False):
            items = self.rules("海口经济学院", [{"id": 7, "name": "海口经济学院本科论文"}])
            self.assertEqual(items[0]["id"], "dayipaper:7")
            response = TestClient(web.app).post(
                "/api/format", data={"ruleId": "school:hkjjxy", "title": "t", "authorName": "a"},
                files={"file": ("a.docx", b"PK")})
            self.assertEqual(response.status_code, 400)


class AdminRegisterTests(unittest.TestCase):
    def test_tunnel_or_spoofed_forwarding_is_not_local(self):
        with patch.multiple(web, AFFILIATE_ADMIN_ON=False), \
             patch.object(web.affiliates, "register_admin_token", return_value="tok"):
            client = TestClient(web.app)
            self.assertEqual(client.post("/api/admin/auth/register", json={},
                                         headers={"X-Forwarded-For": "203.0.113.5, 127.0.0.1"}).status_code, 403)
            self.assertEqual(client.post("/api/admin/auth/register", json={}).status_code, 200)


class AdminOrderListTests(PaidDownloadTests):
    def test_format_export_payments_are_listed(self):
        from core import service_billing
        order = alipay.create_order(self.job_id, "论文")
        alipay._mark_paid(order["out_trade_no"], "T9")
        rows = [r for r in service_billing.admin_orders() if r["out_trade_no"] == order["out_trade_no"]]
        self.assertEqual(rows[0]["operation"], "format_export")
        self.assertEqual(rows[0]["state"], "paid")


class LoginLoopTests(unittest.TestCase):
    def basic(self, user, password):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def test_admin_credentials_also_pass_site_login(self):
        with patch.multiple(web, AUTH_ON=True, AUTH_USER="site", AUTH_PASS="site-pass",
                            AFFILIATE_ADMIN_ON=True, AFFILIATE_ADMIN_USER="admin",
                            AFFILIATE_ADMIN_PASS="admin-pass"):
            client = TestClient(web.app)
            admin = self.basic("admin", "admin-pass")
            self.assertEqual(client.get("/api/admin/coupons", headers=admin).status_code, 200)
            self.assertEqual(client.get("/api/alipay", headers=admin).status_code, 200)
            self.assertEqual(client.get("/api/alipay", headers=self.basic("site", "site-pass")).status_code, 200)
            self.assertEqual(client.get("/api/alipay", headers=self.basic("x", "y")).status_code, 401)
            # Site users still cannot reach the admin API.
            self.assertEqual(client.get("/api/admin/coupons",
                                        headers=self.basic("site", "site-pass")).status_code, 401)

    def test_admin_page_only_calls_admin_api(self):
        script = (Path(web.STATIC) / "affiliates.js").read_text(encoding="utf-8")
        for path in __import__("re").findall(r"""(?:api|fetch)\(\s*[`'"](/api/[^`'"?$]*)""", script):
            self.assertTrue(path.startswith("/api/admin/"), path)


if __name__ == "__main__":
    unittest.main()
