"""服务出问题时第一时间发邮件报警。

配置（环境变量）：
    ALERT_EMAIL      收件人，默认 529813342@qq.com
    ALERTS_ENABLED   设为 false 关闭（测试里默认关闭）

用法：alert(key, subject, detail)。
  · 不阻塞请求：在后台线程里发，SMTP 偶尔拒绝登录时自动重试 3 次。
  · 不刷屏：同一个 key 在 QUIET_SECONDS 内只发一封，之后那封会写明期间又发生了几次。
  · 永远不抛异常：报警本身出错只写日志，不能影响业务。
"""
import logging
import os
import socket
import threading
import time
import traceback

from core import mailer

logger = logging.getLogger(__name__)
DEFAULT_TO = "529813342@qq.com"
QUIET_SECONDS = 10 * 60
RETRIES = 3
_last, _lock = {}, threading.Lock()


def recipient():
    return os.environ.get("ALERT_EMAIL", DEFAULT_TO).strip()


def enabled():
    return bool(recipient()) and os.environ.get("ALERTS_ENABLED", "true").strip().lower() != "false"


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True, name="alert-mail").start()


def _body(subject, detail, suppressed):
    now = time.time()
    lines = [
        "问题：%s" % subject,
        "时间：%s（北京时间） / %s UTC" % (time.strftime("%F %T", time.gmtime(now + 8 * 3600)),
                                    time.strftime("%F %T", time.gmtime(now))),
        "服务器：%s" % socket.gethostname(),
    ]
    if suppressed:
        lines.append("上一封报警之后，同类问题又发生了 %d 次（为避免刷屏没有逐条发送）。" % suppressed)
    lines += ["", str(detail or "").strip() or "（无更多细节）", "",
              "—— 论文排版站自动报警。同类问题 %d 分钟内只发一封。" % (QUIET_SECONDS // 60)]
    return "\n".join(lines)


def _send(subject, body):
    for attempt in range(RETRIES):
        try:
            mailer.send_text(recipient(), "[论文站报警] " + subject, body)
            return True
        except mailer.MailError as exc:
            logger.warning("alert mail attempt %d failed: %s", attempt + 1, getattr(exc, "detail", exc))
            if attempt + 1 < RETRIES:
                time.sleep(2 * (attempt + 1))
        except Exception:                                      # noqa: BLE001
            logger.exception("alert mail crashed")
            return False
    logger.error("alert email could not be sent after %d attempts: %s", RETRIES, subject)
    return False


def alert(key, subject, detail=""):
    """报警。返回 True 表示这次发了（或排队发了），False 表示被合并或已关闭。"""
    try:
        logger.warning("ALERT %s: %s", subject, str(detail)[:300])
        if not enabled():
            return False
        now = time.time()
        with _lock:
            entry = _last.get(key)
            if entry and now - entry[0] < QUIET_SECONDS:
                entry[1] += 1
                return False
            suppressed = entry[1] if entry else 0
            _last[key] = [now, 0]
        _spawn(_send, subject, _body(subject, detail, suppressed))
        return True
    except Exception:                                          # noqa: BLE001
        logger.exception("alert() failed")
        return False


def alert_exception(key, subject, exc, extra=""):
    """带上异常堆栈报警（最后 3000 个字符）。"""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
    return alert(key, subject, ("%s\n\n" % extra if extra else "") + trace)


def send_now(subject, detail=""):
    """立刻同步发一封（部署后自检用），不受合并限制。"""
    return _send(subject, _body(subject, detail, 0))
