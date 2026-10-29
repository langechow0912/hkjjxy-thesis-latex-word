import base64
from contextlib import closing
import json
import os
import time
import urllib.parse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core import affiliates, alipay, service_billing


class AlipayCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_path = self.root / "app-private.pem"
        self.public_path = self.root / "alipay-public.pem"
        self.private_path.write_bytes(self.private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        self.public_path.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.env = patch.dict(os.environ, {
            "ALIPAY_ENABLED": "true", "ALIPAY_APP_ID": "test-app",
            "ALIPAY_APP_PRIVATE_KEY_FILE": str(self.private_path),
            "ALIPAY_PUBLIC_KEY_FILE": str(self.public_path),
            "ALIPAY_NOTIFY_URL": "https://merchant.example/api/alipay/notify",
            "ALIPAY_SELLER_ID": "seller",
            "ALIPAY_MODE": "sandbox", "ALIPAY_ORDER_DB": str(self.root / "orders.sqlite3"),
            "THESIS_FORMAT_PRICE_CENTS": "2999",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_backend_format_price_changes_actual_orders_but_preserves_existing(self):
        self.assertEqual(alipay.config()['amount_cents'], 2999)
        old = alipay.create_order('format-old-price', '旧论文')
        service_billing.save_prices({**service_billing.DEFAULTS, 'format_export_cents': 1999})
        self.assertEqual(alipay.public_status()['amount_cents'], 1999)
        new = alipay.create_order('format-new-price', '新论文')
        self.assertEqual(new['amount_cents'], 1999)
        self.assertEqual(alipay.create_order('format-old-price', '旧论文')['amount_cents'], old['amount_cents'])

    def test_gateway_request_signature_includes_sign_type(self):
        import base64
        import json
        import urllib.parse
        from io import BytesIO

        response = {"alipay_trade_precreate_response": {
            "code": "10000", "qr_code": "https://qr.example/order"}}
        with patch("core.alipay.urllib.request.urlopen",
                   return_value=BytesIO(json.dumps(response).encode())) as gateway:
            alipay._request("alipay.trade.precreate", {
                "out_trade_no": "LP-test-signature", "total_amount": "29.99",
                "subject": "测试论文",
            })
        request = gateway.call_args.args[0]
        params = dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))
        signature = base64.b64decode(params.pop("sign"), validate=True)
        self.assertEqual(params["sign_type"], "RSA2")
        # Build the expected source independently of the production helper.
        source = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        self.private.public_key().verify(signature, source.encode("utf-8"),
                                         padding.PKCS1v15(), hashes.SHA256())

    def test_notification_signature_excludes_sign_type(self):
        import base64

        source = b"app_id=test-app&out_trade_no=LP-test-notify&total_amount=29.99"
        params = {"app_id": "test-app", "out_trade_no": "LP-test-notify",
                  "total_amount": "29.99", "sign_type": "RSA2"}
        params["sign"] = base64.b64encode(self.private.sign(
            source, padding.PKCS1v15(), hashes.SHA256())).decode("ascii")
        self.assertTrue(alipay.verify(params, self.public_path))
        params["total_amount"] = "0.01"
        self.assertFalse(alipay.verify(params, self.public_path))

    def checkout_params(self, order):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(order["payment_url"]).query))
        signature = base64.b64decode(params.pop("sign"), validate=True)
        source = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        self.private.public_key().verify(signature, source.encode("utf-8"),
                                         padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual(params["method"], "alipay.trade.page.pay")
        self.assertEqual(params["sign_type"], "RSA2")
        self.assertEqual(params["notify_url"], "https://merchant.example/api/alipay/notify")
        biz = json.loads(params["biz_content"])
        self.assertEqual(biz["product_code"], "FAST_INSTANT_TRADE_PAY")
        self.assertEqual(biz["out_trade_no"], order["out_trade_no"])
        return biz

    def test_page_pay_signed_full_price_and_pending_paid_reuse(self):
        with patch("core.alipay.urllib.request.urlopen") as network:
            order = alipay.create_order("job-12345678", "测试论文 & <测试>")
            reused_pending = alipay.create_order("job-12345678", "测试论文")
            self.assertEqual(reused_pending["out_trade_no"], order["out_trade_no"])
            with closing(alipay._connect()) as conn, conn:
                conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS' WHERE out_trade_no=?",
                             (order["out_trade_no"],))
            reused = alipay.create_order("job-12345678", "测试论文")
            network.assert_not_called()
        self.assertEqual(order["amount_cents"], 2999)
        biz = self.checkout_params(order)
        self.assertEqual(biz["total_amount"], "29.99")
        self.assertIn("& <测试>", biz["subject"])
        expires = alipay.datetime.strptime(biz["time_expire"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=alipay.CHINA_TZ)
        self.assertEqual(int(expires.timestamp()), order["created_at"] + 900)
        self.assertEqual(reused["out_trade_no"], order["out_trade_no"])
        self.assertEqual(order["qr_code"], "")
        self.assertIsNone(alipay.qr_png(order["out_trade_no"], "job-12345678"))

    def test_checkout_blocks_wrong_job_expired_paid_and_untrusted_host(self):
        order = alipay.create_order("job-checkout", "测试")
        number = order["out_trade_no"]
        self.assertEqual(alipay.checkout_url(number, "job-checkout"), order["payment_url"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            alipay.checkout_url(number, "another-job")
        with patch("core.alipay.time.time", return_value=order["created_at"] + 900):
            with self.assertRaisesRegex(ValueError, "过期"):
                alipay.checkout_url(number, "job-checkout")
            new = alipay.create_order("job-checkout", "测试")
            self.assertNotEqual(new["out_trade_no"], number)
        with closing(alipay._connect()) as conn, conn:
            conn.execute("UPDATE payment_orders SET payment_url='https://evil.example/pay' WHERE out_trade_no=?", (number,))
        with self.assertRaises(alipay.AlipayError):
            alipay.checkout_url(number, "job-checkout")
        with closing(alipay._connect()) as conn, conn:
            conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS' WHERE out_trade_no=?", (number,))
        with self.assertRaisesRegex(ValueError, "已支付"):
            alipay.checkout_url(number, "job-checkout")

    def test_old_schema_migrates_without_reusing_legacy_qr_order(self):
        # The previous database schema and paid history survive the migration.
        import sqlite3
        with closing(sqlite3.connect(alipay._db_path())) as conn, conn:
            conn.execute("CREATE TABLE payment_orders (out_trade_no TEXT PRIMARY KEY, job_id TEXT NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL, qr_code TEXT NOT NULL, trade_no TEXT, created_at INTEGER NOT NULL, paid_at INTEGER)")
            conn.execute("INSERT INTO payment_orders VALUES(?,?,?,?,?,?,?,?)",
                         ("LP-legacy-order", "job-legacy", 2999, "WAIT_BUYER_PAY", "https://qr.example/old", None, int(time.time()), None))
        old = alipay.get_order("LP-legacy-order")
        self.assertEqual(old["payment_product"], "precreate")
        with self.assertRaisesRegex(ValueError, "旧支付订单"):
            alipay.checkout_url(old["out_trade_no"], "job-legacy")
        new = alipay.create_order("job-legacy", "测试")
        self.assertEqual(new["payment_product"], "page_pay")
        self.assertNotEqual(new["out_trade_no"], old["out_trade_no"])

    def test_checkout_route_redirects_but_does_not_unlock_download(self):
        from fastapi.testclient import TestClient
        from web import app as web
        job = "j-page-pay-test"
        web.JOBS[job] = {"kind": "cloud", "state": "done", "taskId": "task-1", "name": "测试", "school_key": "dayipaper:test"}
        try:
            with TestClient(web.app) as client:
                response = client.post(f"/api/format/jobs/{job}/payment", json={})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertNotIn("qr_image", data)
                redirect = client.get(data["checkout_url"], follow_redirects=False)
                self.assertEqual(redirect.status_code, 303)
                self.assertTrue(redirect.headers["location"].startswith("https://openapi-sandbox.dl.alipaydev.com/gateway.do?"))
                self.assertEqual(redirect.headers["cache-control"], "no-store")
                state = client.get(f"/api/format/jobs/{job}/payment/{data['order_id']}").json()
                self.assertFalse(state["paid"])
                denied = client.get(f"/api/format/jobs/{job}/download?fmt=1&payment_order={data['order_id']}&trade_status=TRADE_SUCCESS")
                self.assertEqual(denied.status_code, 402)
                with patch("core.alipay.time.time", return_value=time.time() + 1000):
                    self.assertTrue(client.get(f"/api/format/jobs/{job}/payment/{data['order_id']}").json()["expired"])
                    self.assertEqual(client.get(data["checkout_url"], follow_redirects=False).status_code, 409)
        finally:
            web.JOBS.pop(job, None)

    def test_notification_requires_valid_signature_and_exact_amount(self):
        conn = alipay._connect()
        try:
            conn.execute("""INSERT INTO payment_orders
                (out_trade_no,job_id,amount_cents,status,qr_code,trade_no,created_at,paid_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("LP261022123456ABCDEF", "job-12345678", 2999,
                 "WAIT_BUYER_PAY", "https://qr.example/order", None, 1, None))
            conn.commit()
        finally:
            conn.close()
        params = {"app_id": "test-app", "out_trade_no": "LP261022123456ABCDEF",
                  "total_amount": "29.99", "trade_status": "TRADE_SUCCESS",
                  "trade_no": "trade-1", "seller_id": "seller"}
        signature = self.private.sign(alipay._signature_source(params).encode("utf-8"),
                                      padding.PKCS1v15(), hashes.SHA256())
        import base64
        params["sign"] = base64.b64encode(signature).decode("ascii")
        self.assertTrue(alipay.apply_notification(params))
        self.assertEqual(alipay.get_order("LP261022123456ABCDEF")["status"], "TRADE_SUCCESS")
        params["total_amount"] = "29.98"
        params["sign"] = base64.b64encode(self.private.sign(
            alipay._signature_source(params).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )).decode("ascii")
        self.assertFalse(alipay.apply_notification(params))

    def test_paid_referral_order_creates_one_pending_commission(self):
        affiliate = affiliates.create_affiliate("测试推广员", code="TESTAFF", commission_rate=10)
        order = alipay.create_order("job-referral-123", "测试论文", affiliate["code"], "autumn")
        params = {"app_id": "test-app", "out_trade_no": order["out_trade_no"],
                  "total_amount": "29.99", "trade_status": "TRADE_SUCCESS",
                  "trade_no": "trade-referral", "seller_id": "seller"}
        import base64
        params["sign"] = base64.b64encode(self.private.sign(
            alipay._signature_source(params).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )).decode("ascii")
        self.assertTrue(alipay.apply_notification(params))
        self.assertTrue(alipay.apply_notification(params))
        commissions = affiliates.list_commissions()
        self.assertEqual(len(commissions), 1)
        self.assertEqual(commissions[0]["order_no"], order["out_trade_no"])
        self.assertEqual(commissions[0]["affiliate_id"], affiliate["id"])
        self.assertEqual(commissions[0]["campaign"], "autumn")
        self.assertEqual(commissions[0]["amount_cents"], 300)
        self.assertEqual(commissions[0]["status"], "pending")
        affiliates.record_click(affiliate["code"], "autumn")
        affiliates.record_click(affiliate["code"], "autumn")
        summary = affiliates.list_affiliates()[0]
        self.assertEqual(summary["click_count"], 2)
        self.assertEqual(summary["earned_cents"], 300)

    def test_school_coupon_reserves_limit_and_uses_discounted_payment_amount(self):
        coupon = affiliates.create_coupon("HAIKOU10", "percent", 10,
                                          school_key="school:hkjjxy", max_uses=1)
        quote = affiliates.preview_coupon(coupon["code"], "school:hkjjxy", 2999)
        self.assertEqual(quote["discount_cents"], 300)
        self.assertEqual(quote["amount_cents"], 2699)
        with self.assertRaisesRegex(ValueError, "不适用于"):
            affiliates.preview_coupon(coupon["code"], "dayipaper:other", 2999)

        with patch("core.alipay.urllib.request.urlopen") as network:
            order = alipay.create_order("job-coupon-123", "测试论文", coupon_code="haikou10",
                                        school_key="school:hkjjxy")
            self.assertEqual(self.checkout_params(order)["total_amount"], "26.99")
            network.assert_not_called()
            self.assertEqual(order["discount_cents"], 300)
            self.assertEqual(order["coupon_code"], "HAIKOU10")
            with self.assertRaisesRegex(ValueError, "使用次数上限"):
                affiliates.reserve_coupon("HAIKOU10", "school:hkjjxy", 2999, "second-order")

        params = {"app_id": "test-app", "out_trade_no": order["out_trade_no"],
                  "total_amount": "26.99", "trade_status": "TRADE_SUCCESS",
                  "trade_no": "trade-coupon", "seller_id": "seller"}
        import base64
        params["sign"] = base64.b64encode(self.private.sign(
            alipay._signature_source(params).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )).decode("ascii")
        self.assertTrue(alipay.apply_notification(params))
        self.assertEqual(affiliates.list_coupons()[0]["used_count"], 1)

    def test_withdrawal_locks_approved_commission_and_records_manual_payout(self):
        affiliate = affiliates.create_affiliate("提现推广员", code="PAYOUT", commission_rate=10)
        self.assertEqual(affiliates.authenticate_portal(affiliate["portal_token"])["id"], affiliate["id"])
        self.assertIsNone(affiliates.authenticate_portal("invalid-token"))
        conn = affiliates._connect()
        try:
            conn.execute("""INSERT INTO affiliate_commissions
                (order_no,affiliate_id,campaign,basis_cents,commission_bps,amount_cents,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,1,1)""",
                ("LP261023123456PAYOUT", affiliate["id"], "", 2999, 1000, 300, "pending"))
            conn.commit()
            commission_id = conn.execute("SELECT id FROM affiliate_commissions").fetchone()["id"]
        finally:
            conn.close()
        with self.assertRaisesRegex(ValueError, "没有可申请"):
            affiliates.request_withdrawal(affiliate["id"], "支付宝 user@example.com")
        affiliates.update_commission(commission_id, "approved")
        request = affiliates.request_withdrawal(affiliate["id"], "支付宝 user@example.com")
        self.assertEqual(request["amount_cents"], 300)
        self.assertEqual(affiliates.affiliate_summary(affiliate["id"])["available_cents"], 0)
        with self.assertRaisesRegex(ValueError, "已关联提现"):
            affiliates.update_commission(commission_id, "reversed")
        affiliates.update_withdrawal(request["id"], "approved")
        paid = affiliates.finish_withdrawal(request["id"], "manual-transfer-123", "/proofs/receipt.png")
        self.assertEqual(paid["status"], "paid")
        self.assertEqual(paid["payout_ref"], "manual-transfer-123")
        self.assertEqual(affiliates.list_commissions()[0]["status"], "paid")


if __name__ == "__main__":
    unittest.main()
