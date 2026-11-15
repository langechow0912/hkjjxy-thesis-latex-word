"""【阿里云版】海口经济学院本地排版交给海外排版服务器：主站这一侧的客户端。

主站（阿里云）不装 LibreOffice：把原稿发给海外那台装了 LibreOffice 的排版服务器，
轮询进度，完成后把 docx / pdf 拷回主站。拷回之后的一切（预览、付费下载、24 小时清理）
都和本机排版一模一样，主站的付款逻辑一行不用改。

为什么跨境这一段可以放心重试：排版本身免费，付款发生在导出时，卡一下、重传都不会多收钱；
上传带 Idempotency-Key，网络抖动重传也不会在对面排两遍。

配置（环境变量）：
    FORMAT_WORKER_URL         排版服务器地址，如 https://format.example.com
    FORMAT_WORKER_TOKEN       共享密钥；或 FORMAT_WORKER_TOKEN_FILE（Docker 里 /run/secrets/format_worker_token）
两边用同一个密钥。排版服务器那边只要设了密钥、且本机有 LibreOffice，就会开放 /internal/format-worker/*。
"""
import hashlib
import http.client
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger(__name__)

PREFIX = "/internal/format-worker"
HEALTH_TTL = 20            # 健康检查结果缓存多久；掉线后这段时间内新任务直接走云端 API
HEALTH_TIMEOUT = 5
REQUEST_TIMEOUT = 60       # 查询、下载单次读写上限
UPLOAD_TIMEOUT = 120       # 上传原稿单次读写上限（原稿最大 20 MB）
ATTEMPTS = 3               # 上传和下载最多试几次
BACKOFF = (2, 5)           # 两次重试之前各等多少秒


class RemoteError(Exception):
    """排版服务器相关错误的基类。"""


class Unreachable(RemoteError):
    """连不上、超时、连接中途断了、对面 5xx：这一次没法用它，可以改走云端 API。"""


class Gone(RemoteError):
    """对面没有这个任务了（排版服务器重启会丢掉进行中的任务）。"""


class Busy(RemoteError):
    """排版服务器已经满了，请用户稍后再提交（不改走云端：它只是忙，不是坏了）。"""


class Rejected(RemoteError):
    """对面明确拒绝：密钥不对、请求无效。status 是 HTTP 状态码。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _setting(name):
    return os.environ.get(name, "").strip()


def base_url():
    return _setting("FORMAT_WORKER_URL").rstrip("/")


def token():
    value = _setting("FORMAT_WORKER_TOKEN")
    if value:
        return value
    path = _setting("FORMAT_WORKER_TOKEN_FILE")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8-sig").strip()
    return ""


def configured():
    """主站这一侧：设了排版服务器地址和密钥。"""
    return bool(base_url() and token())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None            # 密钥只发给配置的那台，绝不跟着跳转到别处


_opener = build_opener(_NoRedirect())
_sleep = time.sleep            # 测试里替换，免得真等


def _detail(exc):
    try:
        body = json.loads(exc.read(4096).decode("utf-8", "replace"))
        return str(body.get("detail") or "")[:200]
    except (ValueError, OSError, AttributeError):
        return ""


def _bearer():
    return token()


def _call(method, path, data=None, headers=None, timeout=REQUEST_TIMEOUT):
    """发一个请求，返回响应对象（调用方负责关闭）。所有失败都归到 RemoteError 的某个子类。"""
    request = Request(base_url() + PREFIX + path, data=data, method=method,
                      headers={"Authorization": "Bearer " + _bearer(), **(headers or {})})
    try:
        return _opener.open(request, timeout=timeout)
    except HTTPError as exc:
        detail = _detail(exc)
        exc.close()
        if exc.code == 429:
            raise Busy(detail or "当前排版任务较多，请稍后再提交") from None
        if exc.code == 404:
            raise Gone(detail or "排版服务器上没有这个任务") from None
        if exc.code in (401, 403):
            raise Rejected("排版服务器拒绝了请求（密钥不对）", exc.code) from None
        if exc.code >= 500:
            raise Unreachable("排版服务器返回 HTTP %d" % exc.code) from None
        raise Rejected(detail or "排版服务器拒绝了请求（HTTP %d）" % exc.code, exc.code) from None
    except (URLError, OSError, http.client.HTTPException) as exc:
        raise Unreachable("%s: %s" % (type(exc).__name__, getattr(exc, "reason", exc))) from None


# ---------------------------------------------------------------- 健康检查 + 熔断
_state = {"at": 0.0, "ok": None, "busy": False, "reason": ""}
_lock = threading.Lock()


def _note(ok, reason="", busy=False):
    with _lock:
        previous = _state["ok"]
        _state.update(at=time.monotonic(), ok=ok, busy=busy, reason=reason)
    if ok is False and previous is not False:
        logger.warning("format worker is down: %s", reason)
        try:
            from core import alerts
            alerts.alert(("format-worker",), "海外排版服务器连不上，海口经济学院已改走云端 API",
                         "原因：%s\n\n在它恢复之前，新的海口经济学院排版任务会走云端排版；"
                         "恢复后（每 %d 秒检查一次）自动切回。" % (reason, HEALTH_TTL))
        except Exception:                                      # noqa: BLE001  报警失败不能影响排版
            logger.exception("format worker alert failed")
    elif ok and previous is False:
        logger.warning("format worker is back")


def healthy(force=False):
    """排版服务器现在能不能用。结果缓存 HEALTH_TTL 秒；没配置当作不可用。"""
    if not configured():
        return False
    with _lock:
        if not force and _state["ok"] is not None and time.monotonic() - _state["at"] < HEALTH_TTL:
            return bool(_state["ok"])
    try:
        with _call("GET", "/health", timeout=HEALTH_TIMEOUT) as response:
            info = json.loads(response.read(4096))
    except (RemoteError, ValueError, OSError, http.client.HTTPException) as exc:
        _note(False, str(exc) or type(exc).__name__)
        return False
    capacity = info.get("capacity")
    busy = int(info.get("running") or 0) >= (1 if capacity is None else int(capacity))
    _note(bool(info.get("ok")), "" if info.get("ok") else "排版服务器报告不可用", busy)
    return bool(info.get("ok"))


def busy():
    """最近一次健康检查时它是不是已经满了。"""
    with _lock:
        return bool(_state["busy"])


def mark_down(reason):
    """任务过程中发现连不上：立刻判为不可用，新任务马上走云端 API，不用等下一次健康检查。"""
    _note(False, reason)


def reset():
    """清掉缓存的健康状态（测试用）。"""
    with _lock:
        _state.update(at=0.0, ok=None, busy=False, reason="")


# ---------------------------------------------------------------- 任务
def _multipart(boundary, fields, filename, content):
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                  'Content-Type: application/octet-stream\r\n\r\n' % (boundary, filename)).encode("utf-8"))
    parts.extend([content, ("\r\n--%s--\r\n" % boundary).encode()])
    return b"".join(parts)


def submit(data, filename, meta, key):
    """把原稿交给排版服务器，返回对面的任务号。

    key 是幂等键（用主站的任务号）：重传时对面认出来，不会排两遍。
    连不上会重试；对面忙（Busy）或明确拒绝（Rejected）不重试，原样抛出。"""
    boundary = uuid.uuid4().hex
    body = _multipart(boundary, {"meta": json.dumps(meta, ensure_ascii=False)}, filename, data)
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary, "Idempotency-Key": key}
    last = None
    for attempt in range(ATTEMPTS):
        try:
            with _call("POST", "/jobs", data=body, headers=headers, timeout=UPLOAD_TIMEOUT) as response:
                return json.loads(response.read(65536))["id"]
        except Unreachable as exc:
            last = exc
            logger.warning("format worker upload attempt %d failed: %s", attempt + 1, exc)
            if attempt + 1 < ATTEMPTS:
                _sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
        except (ValueError, KeyError):
            last = Unreachable("排版服务器返回了无法识别的内容")
            break
    raise last


def status(job_id):
    """查询任务进度：{state, stage, log, logCount, suggestions, files}。"""
    try:
        with _call("GET", "/jobs/" + job_id) as response:
            return json.loads(response.read(1024 * 1024))
    except (ValueError, OSError, http.client.HTTPException) as exc:
        raise Unreachable("%s: %s" % (type(exc).__name__, exc)) from None


def fetch(job_id, kind, dest):
    """把成品文件拷回主站，边收边算 SHA-256 和对面给的比对；不一致当作没收到。"""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(ATTEMPTS):
        try:
            with _call("GET", "/jobs/%s/files/%s" % (job_id, kind), timeout=REQUEST_TIMEOUT) as response:
                expected = response.headers.get("X-Content-SHA256", "")
                digest = hashlib.sha256()
                with open(part, "wb") as out:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
                        out.write(chunk)
            if expected and digest.hexdigest() != expected:
                raise Unreachable("文件校验不一致（传输中损坏）")
            os.replace(part, dest)
            return dest
        except Unreachable as exc:
            last = exc
        except (OSError, http.client.HTTPException) as exc:
            last = Unreachable("%s: %s" % (type(exc).__name__, exc))
        logger.warning("format worker download %s attempt %d failed: %s", kind, attempt + 1, last)
        if attempt + 1 < ATTEMPTS:
            _sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    raise last


def discard(job_id):
    """成品拷回来之后让对面删掉（释放它的磁盘）；失败无所谓，对面自己也会按 KEEP_JOBS 回收。"""
    try:
        with _call("DELETE", "/jobs/" + job_id, timeout=HEALTH_TIMEOUT):
            pass
    except (RemoteError, OSError, http.client.HTTPException):
        pass
