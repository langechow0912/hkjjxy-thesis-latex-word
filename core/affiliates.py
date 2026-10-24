"""Affiliate attribution and commission ledger stored beside payment orders."""
from __future__ import annotations

import re
import secrets
import sqlite3
import time
import hashlib
from contextlib import closing
from core import alipay


_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


def _connect():
    conn = sqlite3.connect(alipay._db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS payment_orders (
        out_trade_no TEXT PRIMARY KEY, job_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
        status TEXT NOT NULL, qr_code TEXT NOT NULL, trade_no TEXT,
        created_at INTEGER NOT NULL, paid_at INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS affiliates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '',
        code TEXT NOT NULL UNIQUE,
        commission_bps INTEGER NOT NULL DEFAULT 1000 CHECK (commission_bps BETWEEN 0 AND 10000),
        active INTEGER NOT NULL DEFAULT 1,
        portal_token_hash TEXT,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS affiliate_clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        affiliate_id INTEGER NOT NULL REFERENCES affiliates(id),
        campaign TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS affiliate_commissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_no TEXT NOT NULL UNIQUE,
        affiliate_id INTEGER NOT NULL REFERENCES affiliates(id),
        campaign TEXT NOT NULL DEFAULT '',
        basis_cents INTEGER NOT NULL,
        commission_bps INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'approved', 'paid', 'reversed')),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        payout_ref TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coupons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        discount_kind TEXT NOT NULL CHECK (discount_kind IN ('percent','fixed')),
        discount_value INTEGER NOT NULL,
        school_key TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        starts_at INTEGER,
        ends_at INTEGER,
        max_uses INTEGER,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coupon_uses (
        order_no TEXT PRIMARY KEY,
        coupon_id INTEGER NOT NULL REFERENCES coupons(id),
        discount_cents INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('reserved','redeemed','released')),
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        affiliate_id INTEGER NOT NULL REFERENCES affiliates(id),
        amount_cents INTEGER NOT NULL,
        payout_info TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','approved','paid','rejected')),
        payout_ref TEXT NOT NULL DEFAULT '',
        proof_path TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS withdrawal_items (
        withdrawal_id INTEGER NOT NULL REFERENCES withdrawals(id),
        commission_id INTEGER NOT NULL UNIQUE REFERENCES affiliate_commissions(id),
        PRIMARY KEY(withdrawal_id, commission_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS affiliate_admin_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    )""")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(payment_orders)")}
    if "affiliate_id" not in columns:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN affiliate_id INTEGER")
    if "affiliate_code" not in columns:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN affiliate_code TEXT")
    if "affiliate_campaign" not in columns:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN affiliate_campaign TEXT NOT NULL DEFAULT ''")
    if "affiliate_bps" not in columns:
        conn.execute("ALTER TABLE payment_orders ADD COLUMN affiliate_bps INTEGER")
    for name, definition in (
        ("list_price_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("coupon_id", "INTEGER"),
        ("coupon_code", "TEXT"),
        ("discount_cents", "INTEGER NOT NULL DEFAULT 0"),
        ("school_key", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE payment_orders ADD COLUMN {name} {definition}")
    affiliate_columns = {row["name"] for row in conn.execute("PRAGMA table_info(affiliates)")}
    if "portal_token_hash" not in affiliate_columns:
        conn.execute("ALTER TABLE affiliates ADD COLUMN portal_token_hash TEXT")
    return conn


def _normalize_code(code: str) -> str:
    value = (code or "").strip().upper()
    if not _CODE.fullmatch(value):
        raise ValueError("推广码须为 3-32 位大写字母、数字、下划线或连字符")
    return value


def register_admin_token(label: str = "管理员"):
    label = (label or "管理员").strip()[:80]
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM affiliate_admin_tokens LIMIT 1").fetchone():
            raise ValueError("管理员已注册")
        conn.execute("INSERT INTO affiliate_admin_tokens(label,token_hash,created_at) VALUES(?,?,?)",
                      (label or "管理员", digest, int(time.time())))
    return token


def has_admin_token():
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM affiliate_admin_tokens LIMIT 1").fetchone() is not None


def authenticate_admin_token(token: str):
    if not token or len(token) > 128:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM affiliate_admin_tokens WHERE token_hash=?", (digest,)).fetchone() is not None


def _affiliate_dict(row):
    result = dict(row)
    result["active"] = bool(result["active"])
    result.pop("portal_token_hash", None)
    result["commission_rate"] = result.pop("commission_bps") / 100
    return result


def create_affiliate(name: str, email: str = "", code: str | None = None,
                     commission_rate: float = 10.0):
    name = (name or "").strip()
    email = (email or "").strip()
    if not name or len(name) > 100 or len(email) > 254:
        raise ValueError("推广员姓名必填且不超过 100 字，邮箱不超过 254 字")
    try:
        rate = float(commission_rate)
    except (TypeError, ValueError):
        raise ValueError("佣金比例须为 0 到 100 之间的数字") from None
    if not 0 <= rate <= 100:
        raise ValueError("佣金比例须为 0 到 100 之间的数字")
    code = _normalize_code(code) if code else "P" + secrets.token_hex(5).upper()
    portal_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(portal_token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO affiliates(name,email,code,commission_bps,portal_token_hash,created_at) VALUES(?,?,?,?,?,?)",
            (name, email, code, round(rate * 100), token_hash, int(time.time())))
        row = conn.execute("SELECT * FROM affiliates WHERE id=?", (cursor.lastrowid,)).fetchone()
    result = _affiliate_dict(row)
    result["portal_token"] = portal_token
    return result


def list_affiliates():
    with closing(_connect()) as conn:
        rows = conn.execute("""SELECT a.*,
            (SELECT COUNT(*) FROM affiliate_clicks c WHERE c.affiliate_id=a.id) AS click_count,
            (SELECT COUNT(*) FROM affiliate_commissions m WHERE m.affiliate_id=a.id) AS commission_count,
            (SELECT COALESCE(SUM(m.amount_cents),0) FROM affiliate_commissions m
                WHERE m.affiliate_id=a.id AND m.status != 'reversed') AS earned_cents
            FROM affiliates a ORDER BY a.created_at DESC, a.id DESC""").fetchall()
    return [_affiliate_dict(row) for row in rows]


def update_affiliate(affiliate_id: int, *, active: bool | None = None,
                     commission_rate: float | None = None):
    updates, values = [], []
    if active is not None:
        updates.append("active=?")
        values.append(int(active))
    if commission_rate is not None:
        try:
            rate = float(commission_rate)
        except (TypeError, ValueError):
            raise ValueError("佣金比例须为 0 到 100 之间的数字") from None
        if not 0 <= rate <= 100:
            raise ValueError("佣金比例须为 0 到 100 之间的数字")
        updates.append("commission_bps=?")
        values.append(round(rate * 100))
    if not updates:
        raise ValueError("至少提供一个要更新的字段")
    values.append(affiliate_id)
    with closing(_connect()) as conn, conn:
        result = conn.execute(f"UPDATE affiliates SET {', '.join(updates)} WHERE id=?", values)
        if not result.rowcount:
            return None
        row = conn.execute("SELECT * FROM affiliates WHERE id=?", (affiliate_id,)).fetchone()
    return _affiliate_dict(row)


def get_active_affiliate(code: str):
    try:
        code = _normalize_code(code)
    except ValueError:
        return None
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM affiliates WHERE code=? AND active=1", (code,)).fetchone()
    return _affiliate_dict(row) if row else None


def record_click(code: str, campaign: str = ""):
    affiliate = get_active_affiliate(code)
    if not affiliate:
        return None
    campaign = (campaign or "").strip()[:100]
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO affiliate_clicks(affiliate_id,campaign,created_at) VALUES(?,?,?)",
                     (affiliate["id"], campaign, int(time.time())))
    return {"id": affiliate["id"], "code": affiliate["code"],
            "commission_bps": round(affiliate["commission_rate"] * 100),
            "campaign": campaign}


def snapshot_for_order(code: str | None, campaign: str = ""):
    if not code:
        return None
    affiliate = get_active_affiliate(code)
    if not affiliate:
        return None
    return {"id": affiliate["id"], "code": affiliate["code"],
            "commission_bps": round(affiliate["commission_rate"] * 100),
            "campaign": (campaign or "").strip()[:100]}


def list_commissions(status: str | None = None):
    query = """SELECT m.*, a.name AS affiliate_name, a.code AS affiliate_code
        FROM affiliate_commissions m JOIN affiliates a ON a.id=m.affiliate_id"""
    values = []
    if status:
        if status not in ("pending", "approved", "paid", "reversed"):
            raise ValueError("佣金状态无效")
        query += " WHERE m.status=?"
        values.append(status)
    query += " ORDER BY m.created_at DESC, m.id DESC LIMIT 500"
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(query, values).fetchall()]


def update_commission(commission_id: int, status: str, payout_ref: str = ""):
    if status not in ("approved", "reversed"):
        raise ValueError("佣金只能审核或撤销；结算请通过提现申请登记凭证")
    payout_ref = (payout_ref or "").strip()[:120]
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM affiliate_commissions WHERE id=?", (commission_id,)).fetchone()
        if not row:
            return None
        linked = conn.execute("SELECT 1 FROM withdrawal_items WHERE commission_id=?", (commission_id,)).fetchone()
        allowed = {"pending": {"approved", "reversed"}, "approved": {"reversed"},
                   "paid": set(), "reversed": set()}
        if linked:
            raise ValueError("佣金已关联提现申请，不能单独修改")
        if status not in allowed[row["status"]]:
            raise ValueError("佣金状态只能审核待审核记录，或撤销未结算记录")
        conn.execute("UPDATE affiliate_commissions SET status=?, payout_ref=?, updated_at=? WHERE id=?",
                     (status, payout_ref, int(time.time()), commission_id))
        return dict(conn.execute("SELECT * FROM affiliate_commissions WHERE id=?", (commission_id,)).fetchone())


def settle_paid_order(conn: sqlite3.Connection, order):
    affiliate_id = order["affiliate_id"]
    bps = order["affiliate_bps"]
    if not affiliate_id or bps is None:
        return
    amount = (int(order["amount_cents"]) * int(bps) + 5000) // 10000
    if amount <= 0:
        return
    now = int(time.time())
    conn.execute("""INSERT OR IGNORE INTO affiliate_commissions
        (order_no,affiliate_id,campaign,basis_cents,commission_bps,amount_cents,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,'pending',?,?)""",
        (order["out_trade_no"], affiliate_id, order["affiliate_campaign"] or "",
         order["amount_cents"], bps, amount, now, now))


def _coupon_code(code: str) -> str:
    value = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", value):
        raise ValueError("优惠码须为 3-32 位字母、数字、下划线或连字符")
    return value


def create_coupon(code: str, discount_kind: str, discount_value: float,
                  school_key: str | None = None, max_uses: int | None = None):
    code = _coupon_code(code)
    if discount_kind not in ("percent", "fixed"):
        raise ValueError("优惠类型无效")
    try:
        value = float(discount_value)
    except (TypeError, ValueError):
        raise ValueError("优惠额度无效") from None
    if discount_kind == "percent":
        if not 0 < value < 100:
            raise ValueError("折扣比例须大于 0 且小于 100")
        stored_value = round(value * 100)
    else:
        if not 0 < value <= 10000:
            raise ValueError("固定优惠额须大于 0 且不超过 10000 元")
        stored_value = round(value * 100)
    school_key = (school_key or "").strip() or None
    if school_key and (len(school_key) > 200 or not re.fullmatch(r"(school|dayipaper):[A-Za-z0-9_-]{1,180}", school_key)):
        raise ValueError("学校范围格式应为 school:<院校ID> 或 dayipaper:<规则ID>")
    if max_uses is not None and int(max_uses) < 1:
        raise ValueError("使用次数上限须为正整数")
    with closing(_connect()) as conn, conn:
        cursor = conn.execute("""INSERT INTO coupons
            (code,discount_kind,discount_value,school_key,max_uses,created_at)
            VALUES(?,?,?,?,?,?)""",
            (code, discount_kind, stored_value, school_key, max_uses, int(time.time())))
        row = conn.execute("SELECT * FROM coupons WHERE id=?", (cursor.lastrowid,)).fetchone()
    return _coupon_dict(row)


def _coupon_dict(row):
    result = dict(row)
    result["active"] = bool(result["active"])
    result["discount_value"] = result["discount_value"] / (100 if result["discount_kind"] == "percent" else 100)
    return result


def list_coupons():
    now = int(time.time())
    with closing(_connect()) as conn:
        rows = conn.execute("""SELECT c.*,
            (SELECT COUNT(*) FROM coupon_uses u WHERE u.coupon_id=c.id AND
                (u.status='redeemed' OR (u.status='reserved' AND u.expires_at>?))) AS used_count
            FROM coupons c ORDER BY c.created_at DESC,c.id DESC""", (now,)).fetchall()
    return [_coupon_dict(row) for row in rows]


def update_coupon(coupon_id: int, active: bool):
    with closing(_connect()) as conn, conn:
        result = conn.execute("UPDATE coupons SET active=? WHERE id=?", (int(active), coupon_id))
        if not result.rowcount:
            return None
        return _coupon_dict(conn.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,)).fetchone())


def _coupon_quote(conn, code: str, school_key: str, amount_cents: int, *, reserve_order=None):
    code = _coupon_code(code)
    now = int(time.time())
    row = conn.execute("SELECT * FROM coupons WHERE code=? AND active=1", (code,)).fetchone()
    if not row:
        raise ValueError("优惠码不存在或已停用")
    if row["school_key"] and row["school_key"] != school_key:
        raise ValueError("该优惠码不适用于当前学校规范")
    if row["starts_at"] and now < row["starts_at"]:
        raise ValueError("该优惠码尚未生效")
    if row["ends_at"] and now > row["ends_at"]:
        raise ValueError("该优惠码已过期")
    if row["max_uses"] is not None:
        used = conn.execute("""SELECT COUNT(*) FROM coupon_uses WHERE coupon_id=? AND
            (status='redeemed' OR (status='reserved' AND expires_at>?))""",
            (row["id"], now)).fetchone()[0]
        if used >= row["max_uses"]:
            raise ValueError("该优惠码已达到使用次数上限")
    if row["discount_kind"] == "percent":
        discount = (amount_cents * row["discount_value"] + 5000) // 10000
    else:
        discount = row["discount_value"]
    discount = min(discount, amount_cents - 1)
    if discount <= 0:
        raise ValueError("优惠金额必须小于订单金额")
    quote = {"coupon_id": row["id"], "coupon_code": row["code"],
             "discount_cents": discount, "amount_cents": amount_cents - discount}
    if reserve_order:
        conn.execute("""INSERT INTO coupon_uses
            (order_no,coupon_id,discount_cents,status,expires_at,created_at)
            VALUES(?,?,?,'reserved',?,?)""",
            (reserve_order, row["id"], discount, now + 20 * 60, now))
    return quote


def preview_coupon(code: str, school_key: str, amount_cents: int):
    with closing(_connect()) as conn:
        return _coupon_quote(conn, code, school_key, amount_cents)


def reserve_coupon(code: str | None, school_key: str, amount_cents: int, order_no: str):
    if not code:
        return {"coupon_id": None, "coupon_code": None, "discount_cents": 0,
                "amount_cents": amount_cents}
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        conn.execute("UPDATE coupon_uses SET status='released' WHERE status='reserved' AND expires_at<=?", (now,))
        return _coupon_quote(conn, code, school_key, amount_cents, reserve_order=order_no)


def release_coupon(order_no: str):
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE coupon_uses SET status='released' WHERE order_no=? AND status='reserved'", (order_no,))


def redeem_coupon(conn: sqlite3.Connection, order_no: str):
    conn.execute("UPDATE coupon_uses SET status='redeemed' WHERE order_no=? AND status='reserved'", (order_no,))


def rotate_portal_token(affiliate_id: int):
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn, conn:
        result = conn.execute("UPDATE affiliates SET portal_token_hash=? WHERE id=? AND active=1",
                              (digest, affiliate_id))
        if not result.rowcount:
            return None
    return token


def authenticate_portal(token: str):
    if not token or len(token) > 128:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM affiliates WHERE portal_token_hash=? AND active=1", (digest,)).fetchone()
    return _affiliate_dict(row) if row else None


def affiliate_summary(affiliate_id: int):
    with closing(_connect()) as conn:
        totals = conn.execute("""SELECT
            COALESCE(SUM(CASE WHEN status='pending' THEN amount_cents ELSE 0 END),0) AS pending_cents,
            COALESCE(SUM(CASE WHEN status='approved' AND id NOT IN
                (SELECT commission_id FROM withdrawal_items) THEN amount_cents ELSE 0 END),0) AS available_cents,
            COALESCE(SUM(CASE WHEN status='paid' THEN amount_cents ELSE 0 END),0) AS paid_cents
            FROM affiliate_commissions WHERE affiliate_id=?""", (affiliate_id,)).fetchone()
        rows = conn.execute("SELECT id,amount_cents,status,created_at,updated_at,payout_ref,proof_path FROM withdrawals WHERE affiliate_id=? ORDER BY id DESC LIMIT 50",
                            (affiliate_id,)).fetchall()
        open_request = conn.execute("SELECT 1 FROM withdrawals WHERE affiliate_id=? AND status IN ('pending','approved') LIMIT 1",
                                    (affiliate_id,)).fetchone()
    withdrawals = [dict(row) for row in rows]
    for row in withdrawals:
        row.pop("proof_path", None)
    return {**dict(totals), "has_open_withdrawal": bool(open_request), "withdrawals": withdrawals}


def request_withdrawal(affiliate_id: int, payout_info: str):
    payout_info = (payout_info or "").strip()
    if not payout_info or len(payout_info) > 300:
        raise ValueError("请填写收款方式和收款账号（不超过 300 字）")
    now = int(time.time())
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        outstanding = conn.execute("SELECT 1 FROM withdrawals WHERE affiliate_id=? AND status IN ('pending','approved')",
                                   (affiliate_id,)).fetchone()
        if outstanding:
            raise ValueError("已有提现申请在处理中")
        rows = conn.execute("""SELECT id,amount_cents FROM affiliate_commissions
            WHERE affiliate_id=? AND status='approved' AND id NOT IN
                (SELECT commission_id FROM withdrawal_items) ORDER BY id""", (affiliate_id,)).fetchall()
        amount = sum(row["amount_cents"] for row in rows)
        if not rows:
            raise ValueError("当前没有可申请提现的已审核佣金")
        cursor = conn.execute("""INSERT INTO withdrawals
            (affiliate_id,amount_cents,payout_info,status,created_at,updated_at)
            VALUES(?,?,?,'pending',?,?)""", (affiliate_id, amount, payout_info, now, now))
        conn.executemany("INSERT INTO withdrawal_items(withdrawal_id,commission_id) VALUES(?,?)",
                         [(cursor.lastrowid, row["id"]) for row in rows])
        return dict(conn.execute("SELECT * FROM withdrawals WHERE id=?", (cursor.lastrowid,)).fetchone())


def list_withdrawals(status: str | None = None, affiliate_id: int | None = None):
    if status and status not in ("pending", "approved", "paid", "rejected"):
        raise ValueError("提现状态无效")
    query = """SELECT w.*,a.name AS affiliate_name,a.code AS affiliate_code
        FROM withdrawals w JOIN affiliates a ON a.id=w.affiliate_id WHERE 1=1"""
    values = []
    if status:
        query += " AND w.status=?"; values.append(status)
    if affiliate_id is not None:
        query += " AND w.affiliate_id=?"; values.append(affiliate_id)
    query += " ORDER BY w.created_at DESC,w.id DESC LIMIT 500"
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(query, values).fetchall()]


def update_withdrawal(withdrawal_id: int, status: str):
    if status not in ("approved", "rejected"):
        raise ValueError("提现申请只能审核通过或驳回")
    now = int(time.time())
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row:
            return None
        if row["status"] != "pending":
            raise ValueError("仅待审核的提现申请可处理")
        conn.execute("UPDATE withdrawals SET status=?,updated_at=? WHERE id=?", (status, now, withdrawal_id))
        if status == "rejected":
            conn.execute("DELETE FROM withdrawal_items WHERE withdrawal_id=?", (withdrawal_id,))
        return dict(conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone())


def finish_withdrawal(withdrawal_id: int, payout_ref: str, proof_path: str):
    payout_ref = (payout_ref or "").strip()[:120]
    if not payout_ref or not proof_path:
        raise ValueError("请登记转账流水号并上传凭证")
    now = int(time.time())
    with closing(_connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
        if not row:
            return None
        if row["status"] != "approved":
            raise ValueError("只有审核通过的提现申请可以登记转账")
        conn.execute("UPDATE withdrawals SET status='paid',payout_ref=?,proof_path=?,updated_at=? WHERE id=?",
                     (payout_ref, proof_path, now, withdrawal_id))
        conn.execute("""UPDATE affiliate_commissions SET status='paid',payout_ref=?,updated_at=?
            WHERE id IN (SELECT commission_id FROM withdrawal_items WHERE withdrawal_id=?)
                AND status='approved'""", (payout_ref, now, withdrawal_id))
        return dict(conn.execute("SELECT * FROM withdrawals WHERE id=?", (withdrawal_id,)).fetchone())

