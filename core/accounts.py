"""用户账号、登录会话和「我的记录」。

密码用 scrypt 加盐哈希；会话令牌只存 SHA-256 摘要，数据库泄露也拿不到可用令牌。
数据和支付订单放同一个 SQLite（ALIPAY_ORDER_DB，可用 THESIS_ACCOUNT_DB 单独指定）。
"""
from contextlib import closing
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from core import alipay

USERNAME_RE = re.compile(r"[A-Za-z0-9_一-鿿]{3,20}")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
SESSION_DAYS = 7
COOKIE = "thesis_session"
# 同一用户名 15 分钟内输错 8 次就暂时锁住。站点在反向代理后面，按 IP 限流不可靠。
FAIL_LIMIT, FAIL_WINDOW = 8, 15 * 60
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_FAILS = {}
_FAIL_LOCK = threading.Lock()


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _db_path():
    configured = os.environ.get("THESIS_ACCOUNT_DB", "").strip()
    path = Path(configured) if configured else alipay._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS user_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS user_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL,
            ref TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS user_uploads_by_user ON user_uploads(user_id, created_at);
        CREATE TABLE IF NOT EXISTS email_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_key TEXT NOT NULL,
            purpose TEXT NOT NULL,
            salt TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS email_codes_by_key ON email_codes(email_key, created_at);
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    for name, definition in (("email", "TEXT"), ("email_key", "TEXT"), ("email_verified_at", "INTEGER"),
                             ("points", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            try:
                conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, definition))
            except sqlite3.OperationalError as exc:
                # 部署后第一批并发请求会同时迁移；别的连接先加好了就算完成
                if "duplicate column name" not in str(exc):
                    raise
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users(email_key) WHERE email_key IS NOT NULL")
    conn.commit()
    return conn


def _hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (_SCRYPT["n"], _SCRYPT["r"], _SCRYPT["p"], salt.hex(), digest.hex())


def _check_password(password, stored):
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=32,
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# 用户名不存在时也算一遍哈希，避免靠响应时间判断账号是否存在。
_DUMMY_HASH = _hash_password(secrets.token_hex(8))


def _validate(username, password):
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise AccountError("用户名为 3–20 位字母、数字、下划线或汉字")
    if not PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX:
        raise AccountError("密码长度为 %d–%d 位" % (PASSWORD_MIN, PASSWORD_MAX))
    return username


def _new_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now,))
    conn.execute("INSERT INTO user_sessions VALUES (?,?,?,?)",
                 (_token_hash(token), user_id, now, now + SESSION_DAYS * 86400))
    return token


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def register(username, password):
    """注册并直接登录。返回 (会话令牌, 用户)"""
    if len(password or "") > PASSWORD_MAX:
        raise AccountError("密码长度为 %d–%d 位" % (PASSWORD_MIN, PASSWORD_MAX))
    username = _validate(username, password)
    with closing(_connect()) as conn, conn:
        try:
            user_id = conn.execute("INSERT INTO users (username,password_hash,created_at) VALUES (?,?,?)",
                                   (username, _hash_password(password), int(time.time()))).lastrowid
        except sqlite3.IntegrityError:
            raise AccountError("用户名已被注册", 409) from None
        return _new_session(conn, user_id), {"id": user_id, "username": username}


def _throttled(key):
    now = time.time()
    with _FAIL_LOCK:
        for name in [k for k, v in _FAILS.items() if not v or v[-1] < now - FAIL_WINDOW]:
            _FAILS.pop(name, None)
        recent = [t for t in _FAILS.get(key, []) if t > now - FAIL_WINDOW]
        _FAILS[key] = recent
        return len(recent) >= FAIL_LIMIT


def _record_failure(key):
    with _FAIL_LOCK:
        _FAILS.setdefault(key, []).append(time.time())


def login(username, password):
    """校验密码并新建会话。返回 (会话令牌, 用户)"""
    username = (username or "").strip()
    key = username.lower()
    if _throttled(key):
        raise AccountError("密码错误次数过多，请 15 分钟后再试", 429)
    password = (password or "")[:PASSWORD_MAX + 1]
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT id, username, password_hash FROM users WHERE username=?",
                           (username,)).fetchone()
        if not _check_password(password, row["password_hash"] if row else _DUMMY_HASH) or not row:
            _record_failure(key)
            raise AccountError("用户名或密码错误", 401)
        return _new_session(conn, row["id"]), {"id": row["id"], "username": row["username"]}


def points_display(units):
    """库里的积分是整数「0.1 积分」；对外显示成积分数，整数时不带小数（990 -> 99，99 -> 9.9）。"""
    units = int(units or 0)
    return units // 10 if units % 10 == 0 else units / 10


def session_user(token):
    if not token:
        return None
    with closing(_connect()) as conn:
        row = conn.execute("""SELECT u.id, u.username, u.email, u.points FROM user_sessions s JOIN users u ON u.id=s.user_id
                              WHERE s.token_hash=? AND s.expires_at>=? AND s.created_at>=?""",
                           (_token_hash(token), int(time.time()),
                            int(time.time()) - SESSION_DAYS * 86400)).fetchone()   # 之前签发的 30 天会话也按 7 天截断
    if not row:
        return None
    user = dict(row)
    user["points"] = points_display(user["points"])
    return user


# ---- 邮箱验证码 ----
CODE_TTL = 10 * 60
CODE_COOLDOWN = 60
CODE_HOURLY = 5
CODE_MAX_ATTEMPTS = 5
UNUSABLE_HASH = "!"          # 邮箱注册的账号没有密码；_check_password 对它永远返回 False
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")


def daily_email_cap():
    value = os.environ.get("THESIS_EMAIL_DAILY_CAP", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else 1000


def normalize_email(email):
    """返回 (规范邮箱, 去重键)。去重键去掉 +标签，避免同一个邮箱注册出很多账号。"""
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise AccountError("请输入有效的邮箱地址")
    local, _, domain = email.partition("@")
    return email, local.split("+", 1)[0] + "@" + domain


def _code_hash(salt, key, code):
    return hashlib.sha256(("%s:%s:%s" % (salt, key, code)).encode()).hexdigest()


def send_email_code(email, purpose="login", user_id=None):
    """生成并发送验证码。无论邮箱是否已注册都返回同样结果，不泄露账号是否存在。"""
    from core import mailer
    email, key = normalize_email(email)
    now = int(time.time())
    with closing(_connect()) as conn, conn:
        recent = conn.execute("SELECT created_at FROM email_codes WHERE email_key=? AND created_at>? ORDER BY created_at DESC",
                              (key, now - 3600)).fetchall()
        if recent and now - recent[0]["created_at"] < CODE_COOLDOWN:
            raise AccountError("发送太频繁，请 %d 秒后再试" % (CODE_COOLDOWN - (now - recent[0]["created_at"])), 429)
        if len(recent) >= CODE_HOURLY:
            raise AccountError("该邮箱验证码请求过多，请 1 小时后再试", 429)
        today = conn.execute("SELECT COUNT(*) FROM email_codes WHERE created_at>?", (now - 86400,)).fetchone()[0]
        if today >= daily_email_cap():
            raise AccountError("今日验证码发送量已达上限，请明天再试", 429)
        if purpose == "bind":
            taken = conn.execute("SELECT id FROM users WHERE email_key=? AND id!=?", (key, user_id)).fetchone()
            if taken:
                return {"sent": True}            # 看起来一样，但不发信
        code = "%06d" % secrets.randbelow(10 ** 6)
        salt = secrets.token_hex(8)
        row_id = conn.execute("""INSERT INTO email_codes (email_key,purpose,salt,code_hash,created_at,expires_at)
                                 VALUES (?,?,?,?,?,?)""",
                              (key, purpose, salt, _code_hash(salt, key, code), now, now + CODE_TTL)).lastrowid
    try:
        mailer.send_code(email, code, "绑定邮箱" if purpose == "bind" else "登录")
    except mailer.MailError as exc:
        with closing(_connect()) as conn, conn:        # 没发出去就别占用冷却时间
            conn.execute("DELETE FROM email_codes WHERE id=?", (row_id,))
        raise AccountError(str(exc), exc.status) from None
    return {"sent": True}


def _consume_code(key, purpose, code):
    """校验并一次性消耗验证码。错误次数写库后才返回，防止靠重试穷举。"""
    code = (code or "").strip()
    now = int(time.time())
    with closing(_connect()) as conn, conn:
        row = conn.execute("""SELECT * FROM email_codes WHERE email_key=? AND purpose=? AND used=0 AND expires_at>?
                              ORDER BY created_at DESC LIMIT 1""", (key, purpose, now)).fetchone()
        if not row or row["attempts"] >= CODE_MAX_ATTEMPTS:
            return False
        if not code.isdigit() or not hmac.compare_digest(_code_hash(row["salt"], key, code), row["code_hash"]):
            conn.execute("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            return False
        conn.execute("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
        return True


_BAD_CODE = "验证码错误或已失效"


def login_with_email(email, code):
    """验证码登录；邮箱第一次出现就自动注册。返回 (会话令牌, 用户, 是否新建)"""
    email, key = normalize_email(email)
    if not _consume_code(key, "login", code):
        raise AccountError(_BAD_CODE, 401)
    now = int(time.time())
    created = False
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT id, username, email, points FROM users WHERE email_key=?", (key,)).fetchone()
        if row:
            user_id = row["id"]
        else:
            try:
                user_id = conn.execute("""INSERT INTO users (username,password_hash,created_at,email,email_key,email_verified_at)
                                          VALUES (?,?,?,?,?,?)""",
                                       (email, UNUSABLE_HASH, now, email, key, now)).lastrowid
                created = True
            except sqlite3.IntegrityError:
                raise AccountError("登录失败，请重试", 409) from None
        token = _new_session(conn, user_id)
    if created:
        from core import points
        points.grant_signup_bonus(user_id)
    user = session_user(token)
    return token, user, created


def bind_email(user_id, email, code):
    """老账号补绑邮箱。邮箱已被别人占用时返回通用失败。"""
    email, key = normalize_email(email)
    if not _consume_code(key, "bind", code):
        raise AccountError(_BAD_CODE, 401)
    with closing(_connect()) as conn, conn:
        try:
            changed = conn.execute("""UPDATE users SET email=?, email_key=?, email_verified_at=?
                                      WHERE id=? AND email_key IS NULL""",
                                   (email, key, int(time.time()), user_id)).rowcount
        except sqlite3.IntegrityError:
            raise AccountError(_BAD_CODE, 401) from None
        if not changed:
            raise AccountError("该账号已绑定邮箱", 409)
    return {"email": email}


def logout(token):
    if token:
        with closing(_connect()) as conn, conn:
            conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (_token_hash(token),))


def record_upload(user_id, kind, ref, name, detail=""):
    with closing(_connect()) as conn, conn:
        conn.execute("""INSERT OR IGNORE INTO user_uploads (user_id,kind,ref,name,detail,created_at)
                        VALUES (?,?,?,?,?,?)""",
                     (user_id, kind, ref, (name or "未命名")[:120], (detail or "")[:120], int(time.time())))


def uploads(user_id, limit=100):
    with closing(_connect()) as conn:
        rows = conn.execute("""SELECT kind, ref, name, detail, created_at FROM user_uploads
                               WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?""",
                            (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_upload(user_id, ref):
    with closing(_connect()) as conn:
        row = conn.execute("SELECT kind, ref, name, detail, created_at FROM user_uploads WHERE user_id=? AND ref=?",
                           (user_id, ref)).fetchone()
    return dict(row) if row else None


def paid_order(ref):
    """该任务已付款的订单号；没付款返回 None"""
    with closing(alipay._connect()) as conn:
        row = conn.execute("""SELECT out_trade_no FROM payment_orders WHERE job_id=? AND status='TRADE_SUCCESS'
                              ORDER BY paid_at DESC LIMIT 1""", (ref,)).fetchone()
    return row["out_trade_no"] if row else None
