import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from core import affiliates
from web import app as web


class AffiliateAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "ALIPAY_ORDER_DB": os.path.join(self.temp.name, "orders.sqlite3"),
            "AFFILIATE_PROOF_DIR": os.path.join(self.temp.name, "proofs"),
        })
        self.env.start()
        self.auth = patch.multiple(web, AFFILIATE_ADMIN_ON=False,
                                  AFFILIATE_ADMIN_USER="admin", AFFILIATE_ADMIN_PASS="secret")
        self.auth.start()
        self.client = TestClient(web.app)

    def tearDown(self):
        self.auth.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_admin_shell_is_public_but_api_requires_a_token(self):
        self.assertEqual(self.client.get("/admin/affiliates").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/affiliates").status_code, 401)

    def test_local_first_admin_registration_issues_a_bearer_token_once(self):
        registered = self.client.post("/api/admin/auth/register", json={"name":"本地管理员"})
        self.assertEqual(registered.status_code, 200)
        token = registered.json()["access_token"]
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(self.client.post("/api/admin/auth/register", json={}).status_code, 409)
        verify = self.client.post("/api/admin/auth/verify", headers={"Authorization":f"Bearer {token}"})
        self.assertTrue(verify.json()["valid"])
        response = self.client.get("/api/admin/affiliates", headers={"Authorization":f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        conn = affiliates._connect()
        try:
            stored = conn.execute("SELECT token_hash FROM affiliate_admin_tokens").fetchone()["token_hash"]
            self.assertNotEqual(stored, token)
        finally:
            conn.close()

    def test_admin_registration_rejects_non_loopback_requests(self):
        with patch.object(web, "ipaddress") as ipaddress_mock:
            ipaddress_mock.ip_address.return_value.is_loopback = False
            response = self.client.post("/api/admin/auth/register", json={"name":"外部"},
                                        headers={"X-Forwarded-For":"198.51.100.10"})
        self.assertEqual(response.status_code, 403)

    def test_authenticated_admin_can_create_affiliate(self):
        web.AFFILIATE_ADMIN_ON = True
        try:
            response = self.client.post("/api/admin/affiliates", auth=("admin", "secret"), json={
                "name": "页面测试", "email": "test@example.com", "code": "PAGEAFF", "commission_rate": 12.5,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["code"], "PAGEAFF")
            self.assertEqual(self.client.get("/admin/affiliates", auth=("admin", "secret")).status_code, 200)
            coupon = self.client.post("/api/admin/coupons", auth=("admin", "secret"), json={
                "code": "SCHOOL5", "discount_kind": "fixed", "discount_value": 5,
                "school_key": "school:hkjjxy", "max_uses": 5,
            })
            self.assertEqual(coupon.status_code, 200)
            self.assertEqual(coupon.json()["school_key"], "school:hkjjxy")
        finally:
            web.AFFILIATE_ADMIN_ON = False

    def test_promoter_requests_withdrawal_and_admin_registers_transfer_proof(self):
        affiliate = affiliates.create_affiliate("提现测试", code="WITHDRAW")
        conn = affiliates._connect()
        try:
            conn.execute("""INSERT INTO affiliate_commissions
                (order_no,affiliate_id,campaign,basis_cents,commission_bps,amount_cents,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,1,1)""",
                ("LP261023123456WITHDRAW", affiliate["id"], "", 2999, 1000, 500, "approved"))
            conn.commit()
        finally:
            conn.close()
        portal = self.client.get("/api/affiliate/me", headers={"Authorization": f"Bearer {affiliate['portal_token']}"})
        self.assertEqual(portal.status_code, 200)
        self.assertEqual(portal.json()["available_cents"], 500)
        request = self.client.post("/api/affiliate/withdrawals",
            headers={"Authorization": f"Bearer {affiliate['portal_token']}"},
            json={"payout_info": "支付宝 account@example.com"})
        self.assertEqual(request.status_code, 200)
        withdrawal_id = request.json()["id"]
        web.AFFILIATE_ADMIN_ON = True
        try:
            headers = {"Authorization": "Basic YWRtaW46c2VjcmV0"}
            review = self.client.patch(f"/api/admin/withdrawals/{withdrawal_id}",
                headers=headers, json={"status": "approved"})
            self.assertEqual(review.status_code, 200)
            png = b"\x89PNG\r\n\x1a\nproof"
            paid = self.client.post(f"/api/admin/withdrawals/{withdrawal_id}/paid", headers=headers,
                data={"payout_ref": "manual-987"}, files={"proof": ("receipt.png", png, "image/png")})
            self.assertEqual(paid.status_code, 200)
            self.assertEqual(paid.json()["status"], "paid")
            proof = self.client.get(f"/api/admin/withdrawals/{withdrawal_id}/proof", headers=headers)
            self.assertEqual(proof.content, png)
        finally:
            web.AFFILIATE_ADMIN_ON = False

    def test_referral_redirect_sets_cookie_and_records_campaign_click(self):
        affiliates.create_affiliate("推广员", code="CLICKAFF")
        response = self.client.get("/r/CLICKAFF?campaign=summer", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(response.cookies["affiliate_ref"], "CLICKAFF")
        conn = affiliates._connect()
        try:
            row = conn.execute("SELECT campaign FROM affiliate_clicks").fetchone()
            self.assertEqual(row["campaign"], "summer")
        finally:
            conn.close()

    def test_coupon_quote_and_checkout_use_job_school_and_referral_cookie(self):
        affiliates.create_affiliate("下单归因", code="ORDERREF")
        coupon = affiliates.create_coupon("LOCAL5", "fixed", 5, school_key="school:hkjjxy")
        web.JOBS["j-test-coupon"] = {"id":"j-test-coupon", "kind":"school", "state":"done",
                                     "name":"测试论文", "school_key":"school:hkjjxy"}
        try:
            self.client.get("/r/ORDERREF?campaign=launch", follow_redirects=False)
            with patch.object(web.alipay, "public_status", return_value={"amount_cents":2999,"enabled":True}):
                quote = self.client.post("/api/format/jobs/j-test-coupon/coupon", json={"code":"LOCAL5"})
                self.assertEqual(quote.status_code, 200)
                self.assertEqual(quote.json()["amount_cents"], 2499)
                with patch.object(web.alipay, "create_order", return_value={
                    "out_trade_no":"LP-test", "status":"WAIT_BUYER_PAY", "amount_cents":2499,
                    "list_price_cents":2999, "discount_cents":500, "coupon_code":coupon["code"],
                }) as create_order:
                    result = self.client.post("/api/format/jobs/j-test-coupon/payment", json={"coupon_code":"LOCAL5"})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.json()["discount_cents"], 500)
                    self.assertEqual(create_order.call_args.args[2:], ("ORDERREF", "launch", "LOCAL5", "school:hkjjxy"))
        finally:
            web.JOBS.pop("j-test-coupon", None)


if __name__ == "__main__":
    unittest.main()
