"""Alipay QR checkout for paid cloud-format exports.

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
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


ROOT = Path(__file__).resolve().parent.parent
GATEWAY = "https://openapi.alipay.com/gateway.do"
_ORDER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class AlipayError(Exception):
    pass


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def config():
    app_id = _setting("ALIPAY_APP_ID")
    private_path = Path(_setting("ALIPAY_APP_PRIVATE_KEY_FILE")) if _setting("ALIPAY_APP_PRIVATE_KEY_FILE") else None
    alipay_public_path = Path(_setting("ALIPAY_PUBLIC_KEY_FILE")) if _setting("ALIPAY_PUBLIC_KEY_FILE") else None
    notify_url = _setting("ALIPAY_NOTIFY_URL")
    seller_id = _setting("ALIPAY_SELLER_ID")
    price = _setting("THESIS_FORMAT_PRICE_CENTS")
    enabled = _setting("ALIPAY_ENABLED").lower() == "true"
    ready = bool(enabled and app_id and private_path and private_path.is_file()
                 and alipay_public_path and alipay_public_path.is_file()
                 and notify_url.startswith("https://") and seller_id
                 and _setting("ALIPAY_MODE", "production") in ("production", "sandbox")
                 and price.isdigit()
                 and int(price) > 0)
    return {"enabled": ready, "app_id": app_id, "private_path": private_path,
            "public_path": alipay_public_path, "notify_url": notify_url,
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
    return conn


def _key(path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise AlipayError("支付宝应用私钥读取失败") from exc


def _signature_source(params):
    return "&".join(f"{key}={params[key]}" for key in sorted(params)
                    if key not in ("sign", "sign_type") and params[key] not in (None, ""))


def sign(params, private_path):
    raw = _key(private_path).sign(_signature_source(params).encode("utf-8"),
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


def _request(method, biz_content, cfg=None):
    cfg = cfg or config()
    gateway = GATEWAY
    if cfg["mode"] == "sandbox":
        gateway = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
    params = {"app_id": cfg["app_id"], "method": method, "format": "JSON",
              "charset": "utf-8", "sign_type": "RSA2",
              "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "version": "1.0", "notify_url": cfg["notify_url"],
              "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":"))}
    params["sign"] = sign(params, cfg["private_path"])
    request = urllib.request.Request(gateway, data=urllib.parse.urlencode(params).encode("utf-8"),
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


def create_order(job_id, title):
    cfg = config()
    if not cfg["enabled"]:
        raise AlipayError("支付宝扫码支付未启用或配置不完整")
    with closing(_connect()) as conn, conn:
        existing = conn.execute("""SELECT * FROM payment_orders WHERE job_id=?
            AND status IN ('WAIT_BUYER_PAY','TRADE_SUCCESS')
            ORDER BY CASE WHEN status='TRADE_SUCCESS' THEN 0 ELSE 1 END, created_at DESC LIMIT 1""",
                                (job_id,)).fetchone()
        if existing and (existing["status"] == "TRADE_SUCCESS"
                         or time.time() - existing["created_at"] < 16 * 60):
            return dict(existing)
    order_no = "LP" + datetime.now().strftime("%y%m%d%H%M%S") + os.urandom(5).hex().upper()
    response = _request("alipay.trade.precreate", {
        "out_trade_no": order_no,
        "total_amount": f"{Decimal(cfg['amount_cents']) / 100:.2f}",
        "subject": ("论文排版成品导出：" + title)[:128],
        "timeout_express": "15m",
    }, cfg)
    qr_code = response.get("qr_code")
    if not isinstance(qr_code, str) or not qr_code.startswith("https://"):
        raise AlipayError("支付宝没有返回有效二维码")
    order = {"out_trade_no": order_no, "job_id": job_id,
             "amount_cents": cfg["amount_cents"], "status": "WAIT_BUYER_PAY",
             "qr_code": qr_code, "trade_no": None, "created_at": int(time.time()), "paid_at": None}
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO payment_orders VALUES(?,?,?,?,?,?,?,?)",
                     tuple(order[key] for key in ("out_trade_no", "job_id", "amount_cents", "status", "qr_code", "trade_no", "created_at", "paid_at")))
    return order


def get_order(order_no, job_id=None):
    if not _ORDER_RE.fullmatch(order_no or ""):
        return None
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT * FROM payment_orders WHERE out_trade_no=?", (order_no,)).fetchone()
    if not row or (job_id is not None and row["job_id"] != job_id):
        return None
    return dict(row)


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
    return True


def qr_png(order_no, job_id):
    order = get_order(order_no, job_id)
    if not order:
        return None
    import qrcode
    from io import BytesIO
    image = qrcode.make(order["qr_code"])
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
