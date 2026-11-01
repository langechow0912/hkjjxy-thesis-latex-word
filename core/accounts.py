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
SESSION_DAYS = 30
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
    """)
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


def session_user(token):
    if not token:
        return None
    with closing(_connect()) as conn:
        row = conn.execute("""SELECT u.id, u.username FROM user_sessions s JOIN users u ON u.id=s.user_id
                              WHERE s.token_hash=? AND s.expires_at>=?""",
                           (_token_hash(token), int(time.time()))).fetchone()
    return dict(row) if row else None


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
