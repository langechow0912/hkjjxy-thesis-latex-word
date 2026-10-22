import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from core import alipay


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
            conn.execute("INSERT INTO payment_orders VALUES(?,?,?,?,?,?,?,?)",
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


if __name__ == "__main__":
    unittest.main()
