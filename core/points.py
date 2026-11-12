"""积分：1 元 = 10 积分，用支付宝充值，可抵扣所有服务；积分价与人民币价保持一致（0.99 元 = 9.9 积分）。

计量单位
  * 积分最多一位小数。库里一律存整数「0.1 积分」（也就是 1 分钱 = 1 个单位），没有浮点误差。
  * 本模块对外（路由、管理后台）的积分都是普通数字（9.9、100），进出时在 _units / _show 里换算。
  * 服务的积分价 = 人民币分数，不取整：99 分 = 9.9 积分。

设计要点
  * 余额缓存在 users.points，流水在 points_ledger；二者在同一事务里变动，
    UNIQUE(kind, ref) 让充值入账 / 消费扣减 / 注册赠送都是幂等的。
  * 扣减带 `points >= ?` 条件，余额不可能为负，并发双花只会成功一次。
  * 积分支付最终落成 payment_orders 里一笔 TRADE_SUCCESS 的订单（payment_product='points'），
    下游「该任务是否已付款」的判断（authorize / 我的记录 / 24 小时清理）都不用改。
  * 账号库和订单库默认是同一个 SQLite 文件，但可以分开配置，所以扣积分与写订单是两步：
    订单号由任务号确定性推出，第二步失败时重试同一笔支付会补完订单，不会重复扣分。
"""
from contextlib import closing
import hashlib
import json
import logging
import re
import secrets
import time
import uuid

from core import accounts, alipay

logger = logging.getLogger(__name__)

UNITS_PER_POINT = 10                  # 1 积分 = 10 个单位（0.1 积分）
UNITS_PER_YUAN = 100                  # 1 元 = 10 积分 = 100 个单位 = 100 分
DEFAULT_PACKAGES = (5, 10, 20, 50, 100)
KINDS = ("topup", "spend", "signup_bonus", "admin", "refund", "redeem")


class PointsError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Duplicate(Exception):
    pass


_show = accounts.points_display


def _units(value, name="积分"):
    """把管理员 / 配置里的积分数换成整数单位；超过一位小数、非数字都拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PointsError("%s须为数字" % name, 422)
    scaled = value * UNITS_PER_POINT
    units = round(scaled)
    if abs(scaled - units) > 1e-6:
        raise PointsError("%s最多一位小数" % name, 422)
    return units


def points_for_cents(cents):
    """服务价（分）对应的积分价，与人民币价一致：99 分 = 9.9 积分。"""
    return _show(int(cents))


def _connect():
    conn = accounts._connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS points_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            UNIQUE(kind, ref));
        CREATE INDEX IF NOT EXISTS points_ledger_by_user ON points_ledger(user_id, id);
        CREATE TABLE IF NOT EXISTS point_topups (
            order_no TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            points INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS points_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS redeem_codes (
            code TEXT PRIMARY KEY,
            units INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER,
            disabled INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS redeem_failures (
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS redeem_failures_by_user ON redeem_failures(user_id, created_at);
        CREATE TABLE IF NOT EXISTS ai_holds (
            ref TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            units INTEGER NOT NULL,
            state TEXT NOT NULL,
            charged INTEGER NOT NULL DEFAULT 0,
            tokens INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL);
    """)
    _migrate_units(conn)
    return conn


def _migrate_units(conn):
    """一次性：早期版本按整数积分存，改成 0.1 积分为单位（全部 ×10）。靠 _unit 标记保证只做一次。"""
    if conn.execute("SELECT 1 FROM points_settings WHERE name='_unit'").fetchone():
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT 1 FROM points_settings WHERE name='_unit'").fetchone():
            conn.execute("UPDATE users SET points=points*10")
            conn.execute("UPDATE points_ledger SET delta=delta*10, balance_after=balance_after*10")
            conn.execute("UPDATE point_topups SET points=points*10")
            conn.execute("INSERT INTO points_settings VALUES ('_unit', '\"tenths\"')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ---- 设置（后台可改）----

def _defaults():
    return {"enabled": True, "signup_bonus": 0, "signup_bonus_daily_cap": 100,
            "topup_packages": list(DEFAULT_PACKAGES),
            # AI 助手按实际 token 计费。这三个默认值只是占位，上线前按 DeepSeek 真实账单校准。
            "ai_points_per_1k_tokens": 0.2, "ai_min_charge": 0.5, "ai_max_output_tokens": 3000}


def get_settings():
    values = _defaults()
    with closing(_connect()) as conn:
        for row in conn.execute("SELECT name, value FROM points_settings"):
            if row["name"] in values:
                try:
                    values[row["name"]] = json.loads(row["value"])
                except ValueError:
                    pass
    return values


def save_settings(data):
    if not isinstance(data, dict) or not data or set(data) - set(_defaults()):
        raise PointsError("积分设置的字段不对", 422)
    data = {**get_settings(), **data}          # 没提交的项保持原值，后台新增设置项时旧调用方不会失效
    packages = data["topup_packages"]
    if (not isinstance(packages, list) or not 1 <= len(packages) <= 8
            or any(type(v) is not int or not 1 <= v <= 1000 for v in packages)
            or len(set(packages)) != len(packages)):
        raise PointsError("充值档位须为 1–8 个不重复的 1–1000 元整数", 422)
    bonus_units = _units(data["signup_bonus"], "注册赠送积分")
    if not 0 <= bonus_units <= 10000 * UNITS_PER_POINT:
        raise PointsError("注册赠送积分须为 0–10000", 422)
    cap = data["signup_bonus_daily_cap"]
    if type(cap) is not int or not 0 <= cap <= 100000:
        raise PointsError("每日赠送人数须为 0–100000 的整数", 422)
    if type(data["enabled"]) is not bool:
        raise PointsError("enabled 须为布尔值", 422)
    rate = _units(data["ai_points_per_1k_tokens"], "AI 每千 token 积分")
    if not 1 <= rate <= 100 * UNITS_PER_POINT:
        raise PointsError("AI 每千 token 积分须为 0.1–100", 422)
    least = _units(data["ai_min_charge"], "AI 最低扣费")
    if not 0 <= least <= 100 * UNITS_PER_POINT:
        raise PointsError("AI 最低扣费须为 0–100 积分", 422)
    out = data["ai_max_output_tokens"]
    if type(out) is not int or not 200 <= out <= 8000:
        raise PointsError("AI 单次输出上限须为 200–8000 token", 422)
    clean = {**data, "topup_packages": sorted(packages), "signup_bonus": _show(bonus_units),
             "ai_points_per_1k_tokens": _show(rate), "ai_min_charge": _show(least)}
    with closing(_connect()) as conn, conn:
        conn.executemany("INSERT OR REPLACE INTO points_settings VALUES (?,?)",
                         [(k, json.dumps(v)) for k, v in clean.items()])
    return get_settings()


# ---- 余额与流水 ----

def _apply(conn, user_id, delta, kind, ref, note=""):
    """在已开启的事务里加减（单位：0.1 积分）并记流水。重复的 (kind, ref) 抛 _Duplicate；余额不足抛 PointsError。"""
    if not delta:
        raise PointsError("积分变动不能为 0", 422)
    if conn.execute("SELECT 1 FROM points_ledger WHERE kind=? AND ref=?", (kind, ref)).fetchone():
        raise _Duplicate()
    changed = conn.execute("UPDATE users SET points=points+? WHERE id=? AND points+?>=0",
                           (delta, user_id, delta)).rowcount
    if changed != 1:
        exists = conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        raise PointsError("积分余额不足" if exists else "用户不存在", 402 if exists else 404)
    balance = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()[0]
    conn.execute("""INSERT INTO points_ledger (user_id,delta,balance_after,kind,ref,note,created_at)
                    VALUES (?,?,?,?,?,?,?)""", (user_id, delta, balance, kind, ref, note[:200], int(time.time())))
    return balance


def _transaction(fn):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn(conn)
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def balance(user_id):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()
    return _show(row["points"]) if row else 0


def ledger(user_id, limit=20):
    with closing(_connect()) as conn:
        rows = conn.execute("""SELECT delta, balance_after, kind, ref, note, created_at FROM points_ledger
                               WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit)).fetchall()
    return [{**dict(r), "delta": _show(r["delta"]), "balance_after": _show(r["balance_after"])} for r in rows]


def grant_signup_bonus(user_id):
    """邮箱验证后的新用户赠送一次。按后台设置；有每日人数上限。返回赠送的积分数。"""
    cfg = get_settings()
    units = _units(cfg["signup_bonus"])
    if not cfg["enabled"] or units <= 0:
        return 0

    def run(conn):
        given = conn.execute("SELECT COUNT(*) FROM points_ledger WHERE kind='signup_bonus' AND created_at>?",
                             (int(time.time()) - 86400,)).fetchone()[0]
        if given >= cfg["signup_bonus_daily_cap"]:
            return 0
        _apply(conn, user_id, units, "signup_bonus", str(user_id), "注册赠送")
        return _show(units)
    try:
        return _transaction(run)
    except _Duplicate:
        return 0


def admin_adjust(email, delta, note=""):
    """管理员手动加减积分（也用来给失败订单退积分）。delta 单位是积分，最多一位小数。"""
    units = _units(delta, "积分变动")
    if units == 0 or abs(units) > 1000000 * UNITS_PER_POINT:
        raise PointsError("积分变动须为非零数字，绝对值不超过 1000000", 422)
    _, key = accounts.normalize_email(email)

    def run(conn):
        row = conn.execute("SELECT id FROM users WHERE email_key=?", (key,)).fetchone()
        if not row:
            raise PointsError("没有这个邮箱对应的账号", 404)
        new_balance = _apply(conn, row["id"], units, "admin", "adm-" + uuid.uuid4().hex, note or "管理员调整")
        return {"user_id": row["id"], "balance": _show(new_balance)}
    return _transaction(run)


def admin_ledger(email=None, limit=100):
    with closing(_connect()) as conn:
        sql = """SELECT l.id, u.email, u.username, l.delta, l.balance_after, l.kind, l.ref, l.note, l.created_at
                 FROM points_ledger l JOIN users u ON u.id=l.user_id"""
        args = []
        if email:
            _, key = accounts.normalize_email(email)
            sql += " WHERE u.email_key=?"
            args.append(key)
        rows = conn.execute(sql + " ORDER BY l.id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(r), "delta": _show(r["delta"]), "balance_after": _show(r["balance_after"])} for r in rows]


# ---- 兑换码 ----
# 一个码 = 固定积分数 + 总可用次数（max_uses）+ 可选过期时间；每个用户每个码只能兑一次。
# 入账走 points_ledger（kind='redeem'，ref='码:用户号'），所以「同一人重复兑换」由 UNIQUE(kind, ref) 兜底，
# 「总次数」靠带条件的原子更新兜底，二者与入账在同一事务里：任何一步失败整笔回滚。

REDEEM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉 I O 0 1，抄写不易错
REDEEM_FAIL_LIMIT, REDEEM_FAIL_WINDOW = 10, 15 * 60  # 15 分钟内输错 10 次就先停一停，防暴力猜码
REDEEM_MAX_COUNT = 500


def normalize_code(raw):
    """用户输入 / 后台自定义的码统一成大写字母数字：忽略空格、连字符和大小写。"""
    return re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())


def _show_code(code):
    return "-".join(code[i:i + 4] for i in range(0, 12, 4)) if len(code) == 12 else code


def _code_row(row):
    return {"code": _show_code(row["code"]), "points": _show(row["units"]), "max_uses": row["max_uses"],
            "used": row["used"], "expires_at": row["expires_at"], "disabled": bool(row["disabled"]),
            "note": row["note"], "created_at": row["created_at"]}


def create_redeem_codes(points_each, count=1, max_uses=1, expires_at=None, note="", code=None):
    """后台批量生成兑换码（也可指定一个自定义码）。返回新建的码列表。"""
    units = _units(points_each, "兑换积分")
    if not 1 <= units <= 10000 * UNITS_PER_POINT:
        raise PointsError("每个码的积分须为 0.1–10000", 422)
    if type(count) is not int or not 1 <= count <= REDEEM_MAX_COUNT:
        raise PointsError("一次最多生成 %d 个码" % REDEEM_MAX_COUNT, 422)
    if type(max_uses) is not int or not 1 <= max_uses <= 1000000:
        raise PointsError("可兑换次数须为 1–1000000 的整数", 422)
    now = int(time.time())
    if expires_at is not None and (type(expires_at) is not int or expires_at <= now):
        raise PointsError("过期时间须晚于现在", 422)
    custom = normalize_code(code) if code else None
    if code and not 4 <= len(custom) <= 32:
        raise PointsError("自定义兑换码须为 4–32 位字母或数字", 422)
    if custom and count != 1:
        raise PointsError("自定义兑换码一次只能建一个", 422)
    note = str(note or "")[:100]

    def run(conn):
        made = []
        attempts = 0
        while len(made) < count:
            attempts += 1
            if attempts > count * 20 + 20:
                raise PointsError("生成兑换码失败，请重试", 500)
            candidate = custom or "".join(secrets.choice(REDEEM_ALPHABET) for _ in range(12))
            added = conn.execute("""INSERT OR IGNORE INTO redeem_codes
                                    (code,units,max_uses,expires_at,note,created_at) VALUES (?,?,?,?,?,?)""",
                                 (candidate, units, max_uses, expires_at, note, now)).rowcount
            if added:
                made.append(candidate)
            elif custom:
                raise PointsError("这个兑换码已经存在", 409)
        return made
    return [_show_code(c) for c in _transaction(run)]


def list_redeem_codes(limit=200):
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM redeem_codes ORDER BY created_at DESC, rowid DESC LIMIT ?",
                            (limit,)).fetchall()
    return [_code_row(r) for r in rows]


def set_redeem_disabled(code, disabled):
    key = normalize_code(code)
    with closing(_connect()) as conn, conn:
        changed = conn.execute("UPDATE redeem_codes SET disabled=? WHERE code=?",
                               (1 if disabled else 0, key)).rowcount
    if not changed:
        raise PointsError("没有这个兑换码", 404)
    return {"code": _show_code(key), "disabled": bool(disabled)}


def _redeem_blocked(user_id):
    since = int(time.time()) - REDEEM_FAIL_WINDOW
    with closing(_connect()) as conn:
        failed = conn.execute("SELECT COUNT(*) FROM redeem_failures WHERE user_id=? AND created_at>?",
                              (user_id, since)).fetchone()[0]
    return failed >= REDEEM_FAIL_LIMIT


def _note_redeem_failure(user_id):
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO redeem_failures VALUES (?,?)", (user_id, int(time.time())))
        conn.execute("DELETE FROM redeem_failures WHERE created_at<?", (int(time.time()) - REDEEM_FAIL_WINDOW,))


def redeem(user_id, raw_code):
    """兑换。成功返回 {'points': 到账积分, 'balance': 余额}；码无效、过期、领完、已兑换过都抛 PointsError。"""
    if not get_settings()["enabled"]:
        raise PointsError("积分功能暂未开放", 503)
    code = normalize_code(raw_code)
    if not 4 <= len(code) <= 32:
        raise PointsError("兑换码格式不对", 422)
    if _redeem_blocked(user_id):
        raise PointsError("兑换码输错次数过多，请 15 分钟后再试", 429)

    def run(conn):
        row = conn.execute("SELECT * FROM redeem_codes WHERE code=?", (code,)).fetchone()
        if not row:
            raise PointsError("兑换码无效", 404)
        if row["disabled"]:
            raise PointsError("该兑换码已停用", 410)
        if row["expires_at"] and row["expires_at"] <= time.time():
            raise PointsError("该兑换码已过期", 410)
        try:
            new_balance = _apply(conn, user_id, row["units"], "redeem", "%s:%d" % (code, user_id),
                                 "兑换码 " + _show_code(code))
        except _Duplicate:
            raise PointsError("你已经兑换过这个码了", 409) from None
        if conn.execute("UPDATE redeem_codes SET used=used+1 WHERE code=? AND used<max_uses",
                        (code,)).rowcount != 1:
            raise PointsError("该兑换码已被领完", 410)      # 抛出后整笔回滚，上面的入账一起撤销
        return {"points": _show(row["units"]), "balance": _show(new_balance)}
    try:
        return _transaction(run)
    except PointsError as exc:
        if exc.status == 404:
            _note_redeem_failure(user_id)
        raise


# ---- 充值 ----

def create_topup(user_id, yuan, affiliate_code=None, affiliate_campaign=""):
    cfg = get_settings()
    if not cfg["enabled"]:
        raise PointsError("积分功能暂未开放", 503)
    if type(yuan) is not int or yuan not in cfg["topup_packages"]:
        raise PointsError("请选择充值档位", 422)
    job_id = "PT-" + uuid.uuid4().hex
    try:
        order = alipay.create_order(job_id, "积分充值", affiliate_code, affiliate_campaign,
                                    amount_cents=yuan * 100, resume_return=True)
    except alipay.AlipayError as exc:
        raise PointsError(str(exc), 503) from None
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO point_topups VALUES (?,?,?,?,?)",
                     (order["out_trade_no"], user_id, yuan * UNITS_PER_YUAN, yuan * 100, int(time.time())))
    return {"order_id": order["out_trade_no"], "amount_cents": yuan * 100, "points": _show(yuan * UNITS_PER_YUAN),
            "checkout_url": "/api/points/topup/%s/checkout" % order["out_trade_no"]}


def credit_topup(order_no):
    """充值单已付款 → 入账。幂等；订单金额必须与充值记录一致。"""
    order = alipay.get_order(order_no)
    if not order or order["status"] != "TRADE_SUCCESS" or not order["job_id"].startswith("PT-"):
        return False
    with closing(_connect()) as conn:
        topup = conn.execute("SELECT * FROM point_topups WHERE order_no=?", (order_no,)).fetchone()
    if not topup or topup["amount_cents"] != order["amount_cents"]:
        return False

    def run(conn):
        _apply(conn, topup["user_id"], topup["points"], "topup", order_no, "支付宝充值")
        return True
    try:
        return _transaction(run)
    except _Duplicate:
        return False


TOPUP_SYNC_GRACE = 30 * 60            # 付款页过期后再查这么久，覆盖临近过期才付款、通知延迟的情况


def reconcile(user_id):
    """补记已付款但没入账的充值（进程在标记付款和入账之间崩溃的情况），并补完扣了积分却没落订单的支付。"""
    with closing(_connect()) as conn:
        topups = [(r["order_no"], r["created_at"]) for r in conn.execute(
            """SELECT order_no, created_at FROM point_topups t WHERE user_id=?
               AND NOT EXISTS (SELECT 1 FROM points_ledger WHERE kind='topup' AND ref=t.order_no)""", (user_id,))]
        spends = [(r["ref"], r["note"]) for r in conn.execute(
            "SELECT ref, note FROM points_ledger WHERE user_id=? AND kind='spend' ORDER BY id DESC LIMIT 50", (user_id,))]
    with closing(_connect()) as conn:
        stale = [r["ref"] for r in conn.execute(
            "SELECT ref FROM ai_holds WHERE user_id=? AND state='pending' AND created_at<?",
            (user_id, int(time.time()) - AI_HOLD_TIMEOUT))]
    for ref in stale:                                      # AI 调用早该结束了：进程崩溃遗留的预留，全额退回
        try:
            ai_release(ref, "AI 助手预留超时，已退还")
        except Exception:                                  # noqa: BLE001
            logger.exception("ai hold release failed: %s", ref)
    recent = time.time() - alipay.ORDER_LIFETIME - TOPUP_SYNC_GRACE
    for order_no, created_at in topups:
        try:
            if created_at >= recent:
                # 异步通知可能收不到（本地没有公网地址、通知丢失），付款页回跳或关掉弹窗后
                # 就没人再轮询这笔单；读余额时主动向支付宝确认一次（sync_order 自带频率限制）。
                alipay.sync_order(order_no)
            credit_topup(order_no)
        except Exception:                                  # noqa: BLE001 — 对账失败不该影响读余额
            logger.exception("topup reconcile failed: %s", order_no)
    for job_id, note in spends:
        found = re.search(r"amount_cents=(\d+)", note or "")
        if found and not alipay.job_paid(job_id):
            try:
                # 订单金额必须等于当时的服务价（分），authorize 会核对
                _write_points_order(job_id, int(found.group(1)))
            except Exception:                              # noqa: BLE001
                logger.exception("spend reconcile failed: %s", job_id)


def summary(user_id):
    reconcile(user_id)
    cfg = get_settings()
    return {"enabled": cfg["enabled"], "balance": balance(user_id), "rate": 10,
            "packages": [{"yuan": y, "points": _show(y * UNITS_PER_YUAN)} for y in cfg["topup_packages"]],
            "ledger": ledger(user_id)}


# ---- AI 助手：按实际 token 计费（先预留、后结算）----

AI_HOLD_TIMEOUT = 600


def ai_pricing():
    """当前 AI 计费设置，换成整数单位（0.1 积分）。"""
    cfg = get_settings()
    return {"rate_units": _units(cfg["ai_points_per_1k_tokens"]), "min_units": _units(cfg["ai_min_charge"]),
            "max_output": cfg["ai_max_output_tokens"]}


def _tokens_to_units(tokens, rate_units):
    return -(-int(tokens) * rate_units // 1000)            # 向上取整到 0.1 积分


def ai_reserve_estimate(prompt_chars):
    """调用前要预留多少（单位）。保守上限：每个字符按 1 个 token 算 + 输出上限；实际按用量结算，多退少补。"""
    p = ai_pricing()
    return max(p["min_units"], _tokens_to_units(int(prompt_chars) + p["max_output"], p["rate_units"]))


def ai_reserve(user_id, ref, prompt_chars):
    """预留。余额不足抛 402，不会调用模型。返回预留的积分数。"""
    units = ai_reserve_estimate(prompt_chars)

    def run(conn):
        _apply(conn, user_id, -units, "spend", ref, "AI 助手（预留，按实际用量结算）")
        conn.execute("INSERT INTO ai_holds (ref,user_id,units,state,created_at) VALUES (?,?,?,?,?)",
                     (ref, user_id, units, "pending", int(time.time())))
        return _show(units)
    try:
        return _transaction(run)
    except _Duplicate:
        raise PointsError("重复的请求", 409) from None


def ai_settle(ref, total_tokens):
    """按实际 token 结算：扣 max(最低额, 用量×单价)，不超过预留；多出来的退回。"""
    p = ai_pricing()

    def run(conn):
        hold = conn.execute("SELECT * FROM ai_holds WHERE ref=?", (ref,)).fetchone()
        if not hold or hold["state"] != "pending":
            raise PointsError("这笔预留已经结算过了", 409)
        charged = min(max(p["min_units"], _tokens_to_units(total_tokens, p["rate_units"])), hold["units"])
        back = hold["units"] - charged
        balance_after = None
        if back:
            balance_after = _apply(conn, hold["user_id"], back, "refund", ref,
                                   "AI 助手：按实际用量结算，退还未用部分（%d tokens）" % total_tokens)
        conn.execute("UPDATE ai_holds SET state='settled', charged=?, tokens=? WHERE ref=?",
                     (charged, int(total_tokens), ref))
        return {"charged": _show(charged), "refunded": _show(back), "tokens": int(total_tokens)}
    return _transaction(run)


def ai_release(ref, reason="AI 助手调用失败，已全额退还"):
    """调用失败 / 超时 / 解析失败：全额退回预留。已结算或已退过的不重复退。"""
    def run(conn):
        hold = conn.execute("SELECT * FROM ai_holds WHERE ref=?", (ref,)).fetchone()
        if not hold or hold["state"] != "pending":
            return False
        _apply(conn, hold["user_id"], hold["units"], "refund", ref, reason)
        conn.execute("UPDATE ai_holds SET state='refunded' WHERE ref=?", (ref,))
        return True
    return _transaction(run)


# ---- 积分支付 ----

def _order_no(job_id):
    return "PP" + hashlib.sha256(job_id.encode()).hexdigest()[:24].upper()


def _write_points_order(job_id, amount_cents):
    """落一笔已付款的积分订单；同一任务重复调用只会有一笔。同时关闭该任务还没付的支付宝订单。"""
    order_no = _order_no(job_id)
    now = int(time.time())
    with closing(alipay._connect()) as conn, conn:
        conn.execute("""UPDATE payment_orders SET status='TRADE_CLOSED'
                        WHERE job_id=? AND status='WAIT_BUYER_PAY'""", (job_id,))
        pending = [r["out_trade_no"] for r in conn.execute(
            "SELECT out_trade_no FROM payment_orders WHERE job_id=? AND status='TRADE_CLOSED'", (job_id,))]
        conn.execute("""INSERT OR IGNORE INTO payment_orders
            (out_trade_no,job_id,amount_cents,status,qr_code,trade_no,created_at,paid_at,payment_url,payment_product)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (order_no, job_id, amount_cents, "TRADE_SUCCESS", "", "PTS-" + order_no, now, now, "", "points"))
    for old in pending:                                   # 尽力让支付宝侧也关掉，失败不影响
        try:
            alipay._request("alipay.trade.close", {"out_trade_no": old}, check=False)
        except Exception:                                  # noqa: BLE001
            pass
    return alipay.get_order(order_no)


def pay(user_id, job_id, amount_cents):
    """用积分付 job_id（报价号 TW-… 或排版任务号）。返回已付款订单。

    扣的积分 = 服务价（分）个单位，即 99 分扣 9.9 积分，不取整。
    调用方负责：任务归属、状态可购买、金额来源可信。这里负责：不重复扣、不透支、落订单。
    """
    paid = _paid_order(job_id)
    if paid:
        return paid
    need = int(amount_cents)
    if not get_settings()["enabled"]:
        raise PointsError("积分功能暂未开放", 503)

    def run(conn):
        row = conn.execute("SELECT user_id FROM points_ledger WHERE kind='spend' AND ref=?", (job_id,)).fetchone()
        if row:                                           # 上次扣了积分但订单没落成：直接补订单，不再扣
            if row["user_id"] != user_id:
                raise PointsError("该订单属于其他账号", 403)
            return
        _apply(conn, user_id, -need, "spend", job_id, "积分支付 amount_cents=%d" % amount_cents)
    try:
        _transaction(run)
    except _Duplicate:
        pass
    return _write_points_order(job_id, amount_cents)


def _paid_order(job_id):
    with closing(alipay._connect()) as conn:
        row = conn.execute("""SELECT out_trade_no FROM payment_orders WHERE job_id=? AND status='TRADE_SUCCESS'
                              ORDER BY paid_at DESC LIMIT 1""", (job_id,)).fetchone()
    return alipay.get_order(row["out_trade_no"]) if row else None
