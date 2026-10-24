import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core import affiliates, alipay


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

    def test_precreate_uses_full_price_without_first_order_discount(self):
        with patch.object(alipay, "_request", return_value={"qr_code": "https://qr.example/order"}) as request:
            order = alipay.create_order("job-12345678", "测试论文")
            conn = alipay._connect()
            try:
                conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS' WHERE out_trade_no=?",
                             (order["out_trade_no"],))
                conn.commit()
            finally:
                conn.close()
            reused = alipay.create_order("job-12345678", "测试论文")
        self.assertEqual(order["amount_cents"], 2999)
        self.assertEqual(request.call_args.args[1]["total_amount"], "29.99")
        self.assertTrue(order["qr_code"].startswith("https://"))
        self.assertEqual(reused["out_trade_no"], order["out_trade_no"])
        request.assert_called_once()
        self.assertTrue(alipay.qr_png(order["out_trade_no"], "job-12345678").startswith(b"\x89PNG"))

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
        with patch.object(alipay, "_request", return_value={"qr_code": "https://qr.example/order"}):
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

        with patch.object(alipay, "_request", return_value={"qr_code": "https://qr.example/coupon"}) as request:
            order = alipay.create_order("job-coupon-123", "测试论文", coupon_code="haikou10",
                                        school_key="school:hkjjxy")
            self.assertEqual(request.call_args.args[1]["total_amount"], "26.99")
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
