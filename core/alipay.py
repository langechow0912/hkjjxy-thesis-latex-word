"""Alipay desktop website checkout for paid cloud-format exports.

Checkout stays disabled until the merchant enables it and supplies a complete
production configuration. The application private key is read from its file
path and is never returned by this module.
"""
import base64
from contextlib import closing
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


ROOT = Path(__file__).resolve().parent.parent
GATEWAY = "https://openapi.alipay.com/gateway.do"
_ORDER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ORDER_LIFETIME = 15 * 60
CHINA_TZ = timezone(timedelta(hours=8))


class AlipayError(Exception):
    pass


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def config():
    app_id = _setting("ALIPAY_APP_ID")
    private_path = Path(_setting("ALIPAY_APP_PRIVATE_KEY_FILE")) if _setting("ALIPAY_APP_PRIVATE_KEY_FILE") else None
    alipay_public_path = Path(_setting("ALIPAY_PUBLIC_KEY_FILE")) if _setting("ALIPAY_PUBLIC_KEY_FILE") else None
    notify_url = _setting("ALIPAY_NOTIFY_URL")
    return_url = _setting("ALIPAY_RETURN_URL")
    seller_id = _setting("ALIPAY_SELLER_ID")
    from core import service_billing
    price = str(service_billing.prices()['format_export_cents'])
    enabled = _setting("ALIPAY_ENABLED").lower() == "true"
    ready = bool(enabled and app_id and private_path and private_path.is_file()
                 and alipay_public_path and alipay_public_path.is_file()
                 and notify_url.startswith("https://") and seller_id
                 and _setting("ALIPAY_MODE", "production") in ("production", "sandbox")
                 and price.isdigit()
                 and int(price) > 0)
    return {"enabled": ready, "app_id": app_id, "private_path": private_path,
            "public_path": alipay_public_path, "notify_url": notify_url,
            "return_url": return_url,
            "seller_id": seller_id,
            "amount_cents": int(price) if price.isdigit() else 0,
            "mode": _setting("ALIPAY_MODE", "production")}


def public_status():
    cfg = config()
    return {"enabled": cfg["enabled"], "amount_cents": cfg["amount_cents"] if cfg["enabled"] else None,
            "mode": cfg["mode"] if cfg["enabled"] else None}


def _db_path():
    path = Path(_setting("ALIPAY_ORDER_DB", str(ROOT / ".secrets" / "alipay-orders.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS payment_orders (
        out_trade_no TEXT PRIMARY KEY, job_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
        status TEXT NOT NULL, qr_code TEXT NOT NULL, trade_no TEXT,
        created_at INTEGER NOT NULL, paid_at INTEGER
    )""")
    from core import affiliates
    with closing(affiliates._connect()):
        pass
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(payment_orders)")}
    for name, definition in (
        ("payment_url", "TEXT NOT NULL DEFAULT ''"),
        ("payment_product", "TEXT NOT NULL DEFAULT 'precreate'"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE payment_orders ADD COLUMN {name} {definition}")
    conn.commit()
    return conn


def _key(path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise AlipayError("支付宝应用私钥读取失败") from exc


def _signature_source(params, *, exclude_sign_type=True):
    excluded = {"sign", "sign_type"} if exclude_sign_type else {"sign"}
    return "&".join(f"{key}={params[key]}" for key in sorted(params)
                    if key not in excluded and params[key] not in (None, ""))


def sign(params, private_path):
    # Gateway requests include sign_type; asynchronous notifications exclude it.
    raw = _key(private_path).sign(_signature_source(params, exclude_sign_type=False).encode("utf-8"),
                                  padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(raw).decode("ascii")


def verify(params, public_path):
    signature = params.get("sign")
    if not signature:
        return False
    try:
        key = serialization.load_pem_public_key(public_path.read_bytes())
        key.verify(base64.b64decode(signature, validate=True),
                   _signature_source(params).encode("utf-8"),
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def _gateway(cfg):
    return ("https://openapi-sandbox.dl.alipaydev.com/gateway.do"
            if cfg["mode"] == "sandbox" else GATEWAY)


def _signed_params(method, biz_content, cfg):
    params = {"app_id": cfg["app_id"], "method": method, "format": "JSON",
              "charset": "utf-8", "sign_type": "RSA2",
              "timestamp": datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
              "version": "1.0", "notify_url": cfg["notify_url"],
              "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":"))}
    if method == "alipay.trade.page.pay" and cfg.get("return_url"):
        params["return_url"] = cfg["return_url"]
    params["sign"] = sign(params, cfg["private_path"])
    return params


def _request(method, biz_content, cfg=None):
    cfg = cfg or config()
    params = _signed_params(method, biz_content, cfg)
    request = urllib.request.Request(_gateway(cfg), data=urllib.parse.urlencode(params).encode("utf-8"),
                                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except Exception as exc:
        raise AlipayError("支付宝接口暂不可用，请稍后重试") from exc
    result = data.get(method.replace(".", "_") + "_response", {})
    if result.get("code") != "10000":
        raise AlipayError("支付宝未能创建支付订单，请核对产品开通状态和应用配置")
    return result


def create_order(job_id, title, affiliate_code=None, affiliate_campaign="",
                coupon_code=None, school_key="", *, amount_cents=None, subject_prefix="论文排版成品导出："):
    cfg = config()
    if amount_cents is not None:
        if type(amount_cents) is not int or not 1 <= amount_cents <= 10000000:
            raise ValueError("订单金额无效")
        cfg = {**cfg, "amount_cents": amount_cents}
    if not cfg["enabled"]:
        raise AlipayError("支付宝电脑网站支付未启用或配置不完整")
    from core import affiliates
    with closing(affiliates._connect()):
        pass
    referral = affiliates.snapshot_for_order(affiliate_code, affiliate_campaign)
    requested_coupon = (coupon_code or "").strip().upper() or None
    with closing(_connect()) as conn, conn:
        existing = conn.execute("""SELECT * FROM payment_orders WHERE job_id=?
            AND status IN ('WAIT_BUYER_PAY','TRADE_SUCCESS')
            ORDER BY CASE WHEN status='TRADE_SUCCESS' THEN 0 ELSE 1 END, created_at DESC LIMIT 1""",
                                (job_id,)).fetchone()
        if existing and existing["status"] == "TRADE_SUCCESS":
            return dict(existing)
        if (existing and existing["payment_product"] == "page_pay"
                and time.time() - existing["created_at"] < ORDER_LIFETIME):
            if existing["coupon_code"] != requested_coupon:
                raise AlipayError("该任务已有待支付订单，请完成或等待订单过期后再更换优惠码")
            return dict(existing)
        if existing:
            affiliates.release_coupon(existing["out_trade_no"])
    order_no = "LP" + datetime.now().strftime("%y%m%d%H%M%S") + os.urandom(5).hex().upper()
    try:
        quote = affiliates.reserve_coupon(requested_coupon, school_key, cfg["amount_cents"], order_no)
    except ValueError:
        raise
    created_at = int(time.time())
    try:
        params = _signed_params("alipay.trade.page.pay", {
            "out_trade_no": order_no,
            "total_amount": f"{Decimal(quote['amount_cents']) / 100:.2f}",
            "subject": (subject_prefix + title)[:128],
            "product_code": "FAST_INSTANT_TRADE_PAY",
            "time_expire": datetime.fromtimestamp(created_at + ORDER_LIFETIME, CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }, cfg)
        payment_url = _gateway(cfg) + "?" + urllib.parse.urlencode(params)
    except Exception:
        affiliates.release_coupon(order_no)
        raise
    order = {"out_trade_no": order_no, "job_id": job_id,
             "amount_cents": quote["amount_cents"], "list_price_cents": cfg["amount_cents"],
             "discount_cents": quote["discount_cents"], "coupon_id": quote["coupon_id"],
             "coupon_code": quote["coupon_code"], "school_key": school_key,
             "status": "WAIT_BUYER_PAY",
             "qr_code": "", "payment_url": payment_url, "payment_product": "page_pay",
             "trade_no": None, "created_at": created_at, "paid_at": None,
             "affiliate_id": referral["id"] if referral else None,
             "affiliate_code": referral["code"] if referral else None,
             "affiliate_campaign": referral["campaign"] if referral else "",
             "affiliate_bps": referral["commission_bps"] if referral else None}
    with closing(_connect()) as conn, conn:
        conn.execute("""INSERT INTO payment_orders
            (out_trade_no,job_id,amount_cents,status,qr_code,trade_no,created_at,paid_at,
             affiliate_id,affiliate_code,affiliate_campaign,affiliate_bps,
             list_price_cents,coupon_id,coupon_code,discount_cents,school_key,payment_url,payment_product)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(order[key] for key in ("out_trade_no", "job_id", "amount_cents", "status",
                  "qr_code", "trade_no", "created_at", "paid_at", "affiliate_id",
                  "affiliate_code", "affiliate_campaign", "affiliate_bps", "list_price_cents",
                  "coupon_id", "coupon_code", "discount_cents", "school_key", "payment_url", "payment_product")))
    return order


def get_order(order_no, job_id=None):
    if not _ORDER_RE.fullmatch(order_no or ""):
        return None
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=?", (order_no,)).fetchone()
    if not row or (job_id is not None and row["job_id"] != job_id):
        return None
    return dict(row)


def checkout_url(order_no, job_id):
    order = get_order(order_no, job_id)
    if not config()["enabled"]:
        raise AlipayError("支付宝支付未启用")
    if not order:
        raise ValueError("支付订单不存在")
    if order["status"] != "WAIT_BUYER_PAY":
        raise ValueError("订单已支付或已关闭，请返回原页面查看")
    if order["payment_product"] != "page_pay" or not order["payment_url"]:
        raise ValueError("旧支付订单已停用，请返回原页面重新创建订单")
    if time.time() >= order["created_at"] + ORDER_LIFETIME:
        raise ValueError("支付订单已过期，请返回原页面重新创建订单")
    # Only redirect to the configured official gateway, even for persisted orders.
    url = order["payment_url"]
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(_gateway(config()))
    if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path):
        raise AlipayError("支付链接无效，请重新创建订单")
    return url


def apply_notification(params):
    cfg = config()
    if not cfg["enabled"] or not verify(params, cfg["public_path"]):
        return False
    if params.get("app_id") != cfg["app_id"]:
        return False
    order = get_order(params.get("out_trade_no"))
    expected_amount = f"{Decimal(order['amount_cents']) / 100:.2f}" if order else None
    if not order or params.get("total_amount") != expected_amount:
        return False
    if params.get("seller_id") != cfg["seller_id"]:
        return False
    if params.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        return True
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS', trade_no=?, paid_at=? WHERE out_trade_no=? AND status!='TRADE_SUCCESS'",
                     (params.get("trade_no"), int(time.time()), order["out_trade_no"]))
        from core import affiliates
        paid_order = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=?",
                                  (order["out_trade_no"],)).fetchone()
        affiliates.redeem_coupon(conn, order["out_trade_no"])
        affiliates.settle_paid_order(conn, paid_order)
    return True


def qr_png(order_no, job_id):
    order = get_order(order_no, job_id)
    if not order or not order["qr_code"] or order["payment_product"] != "precreate":
        return None
    import qrcode
    from io import BytesIO
    image = qrcode.make(order["qr_code"])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
