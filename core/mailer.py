"""发验证码邮件。标准库 smtplib，不加依赖。

配置有两种，账号文件优先：

1. 多账号（推荐）：SMTP_ACCOUNTS_FILE 指向一个 JSON 文件（Docker 里是 /run/secrets/smtp_accounts.json）。
   列表的顺序就是优先级：先用第一个，认证失败/连不上/被限流就自动换下一个。
   每次发信都重新读这个文件，加账号、调顺序不用重启。格式见 deploy/secrets/README.md。
       [{"host": "smtp.163.com", "port": 465, "security": "ssl", "user": "a@163.com",
         "password": "授权码" 或 "password_file": "/run/secrets/xxx", "from": "可选，默认等于 user"}]
2. 单账号（兼容老配置）：SMTP_HOST / SMTP_PORT / SMTP_SECURITY(ssl|starttls) / SMTP_USER
   SMTP_PASSWORD 或 SMTP_PASSWORD_FILE / SMTP_FROM（默认等于 SMTP_USER）

没配 SMTP：生产环境（THESIS_ENV=production）直接报错；非生产把验证码打到日志，只给本地调试用。
"""
import json
import logging
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

logger = logging.getLogger(__name__)
TIMEOUT = 10
TOTAL_BUDGET = 30            # 一封信最多折腾这么多秒，账号再多也不让用户等太久
COOLDOWN_AUTH = 10 * 60      # 认证失败（授权码失效/被风控）：先歇 10 分钟
COOLDOWN_OTHER = 60          # 连不上、被限流等：歇 1 分钟
SENDER_NAME = "Paper Layout"

_cooldown = {}               # 账号邮箱 → 冷却到什么时候（time.monotonic）
_lock = threading.Lock()


class MailError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def _read_secret(path):
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8-sig").strip()
    return ""


def _password():
    return _setting("SMTP_PASSWORD") or _read_secret(_setting("SMTP_PASSWORD_FILE"))


def _account(host, port, security, user, password, sender):
    security = (security or "ssl").strip().lower()
    return {"host": host.strip(), "security": security,
            "port": int(port or (465 if security == "ssl" else 587)),
            "user": user.strip(), "password": password, "sender": (sender or user).strip()}


def _file_accounts(path):
    """读账号文件。文件坏了或某一项不完整只记日志、跳过，不能拖垮发信。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            raw = raw.get("accounts", [])
        if not isinstance(raw, list):
            raise ValueError("顶层必须是列表")
    except (OSError, ValueError) as exc:
        logger.error("SMTP accounts file unreadable (%s): %s", path, exc)
        return []
    accounts = []
    for n, item in enumerate(raw, 1):
        try:
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            password = str(item.get("password") or "").strip() or _read_secret(str(item.get("password_file") or ""))
            if not (item.get("host") and item.get("user") and password):
                raise ValueError("缺少 host / user / 授权码")
            accounts.append(_account(item["host"], item.get("port"), item.get("security"),
                                     item["user"], password, item.get("from")))
        except (ValueError, TypeError) as exc:
            logger.error("SMTP accounts file: skip entry #%d (%s)", n, exc)
    return accounts


def accounts():
    """当前可用的发信账号，按优先级从高到低。"""
    path = _setting("SMTP_ACCOUNTS_FILE")
    if path and Path(path).is_file():
        found = _file_accounts(path)
        if found:
            return found
        logger.error("SMTP accounts file has no usable account, falling back to SMTP_* variables")
    host, user, password = _setting("SMTP_HOST"), _setting("SMTP_USER"), _password()
    if not (host and user and password):
        return []
    try:
        return [_account(host, _setting("SMTP_PORT"), _setting("SMTP_SECURITY"), user, password, _setting("SMTP_FROM"))]
    except ValueError as exc:
        logger.error("SMTP_PORT invalid: %s", exc)
        return []


def configured():
    return bool(accounts())


def production():
    return _setting("THESIS_ENV").lower() == "production"


def _message(to, subject, body):
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def _send_via(account, message):
    context = ssl.create_default_context()
    if account["security"] == "ssl":
        client = smtplib.SMTP_SSL(account["host"], account["port"], timeout=TIMEOUT, context=context)
    else:
        client = smtplib.SMTP(account["host"], account["port"], timeout=TIMEOUT)
    with client:
        if account["security"] != "ssl":
            client.starttls(context=context)
        client.login(account["user"], account["password"])
        del message["From"]                              # 发件人必须是登录的那个账号，服务商才肯收
        message["From"] = formataddr((SENDER_NAME, account["sender"]))
        client.send_message(message)


def _cool_down(account, exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused):   # 收件人的问题，不是这个账号的问题
        return
    seconds = COOLDOWN_AUTH if isinstance(exc, smtplib.SMTPAuthenticationError) else COOLDOWN_OTHER
    with _lock:
        _cooldown[account["user"]] = time.monotonic() + seconds


def _order(found):
    """冷却中的账号排到最后：别的都失败时仍然试一遍，免得全部被冷却就彻底发不出去。"""
    now = time.monotonic()
    with _lock:
        cooling = {a["user"] for a in found if _cooldown.get(a["user"], 0) > now}
    return [a for a in found if a["user"] not in cooling] + [a for a in found if a["user"] in cooling]


def deliver(message):
    """按优先级逐个账号发，第一个成功就停；返回实际发出的账号邮箱。

    全部失败抛 MailError，各账号的错误文字（不含授权码）拼在它的 detail 属性里，供调用方报警。
    """
    found = accounts()
    if not found:
        error = MailError("邮件服务未配置")
        error.detail = "没有可用的 SMTP 账号"
        raise error
    started, errors = time.monotonic(), []
    for account in _order(found):
        if errors and time.monotonic() - started > TOTAL_BUDGET:
            errors.append("（已超过 %d 秒，剩余账号未再尝试）" % TOTAL_BUDGET)
            break
        try:
            _send_via(account, message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            _cool_down(account, exc)
            logger.warning("SMTP send failed via %s: %s", account["user"], exc)
            errors.append("%s: %s: %s" % (account["user"], type(exc).__name__, exc))
            continue
        if errors:
            logger.warning("SMTP fell back to %s after %d failed account(s)", account["user"], len(errors))
        return account["user"]
    error = MailError("邮件发送失败，请稍后再试")
    error.detail = "；".join(errors)
    raise error from None


def send_text(to, subject, body):
    """发一封纯文本邮件（报警用）。没配 SMTP 时只写日志。"""
    if not configured():
        logger.warning("[DEV MAIL] → %s : %s\n%s", to, subject, body)
        return
    deliver(_message(to, subject, body))


def send_code(to, code, purpose="登录"):
    """发一封验证码邮件。失败只抛 MailError（对用户的文案是通用的，细节只进日志）。"""
    if not configured():
        if production():
            raise MailError("邮件服务未配置，暂时无法发送验证码", 503)
        logger.warning("[DEV MAIL] %s 验证码 → %s : %s", purpose, to, code)
        return
    message = _message(to, "Paper Layout %s验证码" % purpose,
                       "您的%s验证码是：%s\n\n验证码10分钟内有效，请勿告诉任何人。\n如果不是您本人操作，请忽略这封邮件。" % (purpose, code))
    try:
        deliver(message)
    except MailError as exc:
        from core import alerts
        alerts.alert(("smtp",), "验证码邮件发送失败", "收件人域名：%s\n错误：%s" % (to.rsplit("@", 1)[-1], getattr(exc, "detail", exc)))
        raise MailError("验证码邮件发送失败，请稍后再试") from None
