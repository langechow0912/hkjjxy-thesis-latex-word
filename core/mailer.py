"""发验证码邮件。标准库 smtplib，不加依赖。

配置（环境变量，密码优先走文件）：
    SMTP_HOST / SMTP_PORT / SMTP_SECURITY(ssl|starttls) / SMTP_USER
    SMTP_PASSWORD 或 SMTP_PASSWORD_FILE / SMTP_FROM（默认等于 SMTP_USER）

没配 SMTP：生产环境（THESIS_ENV=production）直接报错；非生产把验证码打到日志，只给本地调试用。
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

logger = logging.getLogger(__name__)
TIMEOUT = 10


class MailError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _setting(name, default=""):
    return os.environ.get(name, default).strip()


def _password():
    value = _setting("SMTP_PASSWORD")
    if value:
        return value
    path = _setting("SMTP_PASSWORD_FILE")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8-sig").strip()
    return ""


def configured():
    return bool(_setting("SMTP_HOST") and _setting("SMTP_USER") and _password())


def production():
    return _setting("THESIS_ENV").lower() == "production"


def send_code(to, code, purpose="登录"):
    """发一封验证码邮件。失败只抛 MailError（对用户的文案是通用的，细节只进日志）。"""
    if not configured():
        if production():
            raise MailError("邮件服务未配置，暂时无法发送验证码", 503)
        logger.warning("[DEV MAIL] %s 验证码 → %s : %s", purpose, to, code)
        return
    message = EmailMessage()
    sender = _setting("SMTP_FROM") or _setting("SMTP_USER")
    message["From"] = formataddr(("Paper Layout", sender))
    message["To"] = to
    message["Subject"] = "Paper Layout %s验证码" % purpose
    message.set_content(
        "您的%s验证码是：%s\n\n10 分钟内有效，请勿告诉任何人。\n如果不是您本人操作，请忽略这封邮件。" % (purpose, code))
    host = _setting("SMTP_HOST")
    security = _setting("SMTP_SECURITY", "ssl").lower()
    try:
        port = int(_setting("SMTP_PORT") or (465 if security == "ssl" else 587))
        context = ssl.create_default_context()
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=TIMEOUT)
        with client:
            if security != "ssl":
                client.starttls(context=context)
            client.login(_setting("SMTP_USER"), _password())
            client.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        logger.warning("SMTP send failed: %s", exc)
        raise MailError("验证码邮件发送失败，请稍后再试") from None
