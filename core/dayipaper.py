# -*- coding: utf-8 -*-
"""大易论文「排版开放接口」客户端：规范搜索 → 上传 → 发起排版 → 轮询 → 建议 → 导出。

为什么要有这一层：本地流水线（docx → tex → XeLaTeX / python-docx）保证「格式可改、产物可控」，
代价是每所学校都要做一份 profile；开放接口解决的是「还没做 profile 的学校也能按官方规范出稿」。
两条通道并存，互不依赖 —— 云端失败不影响本地构建，反之亦然。

认证：PAT 只从环境变量 DAYIPAPER_PAT 读，不落盘、不进日志、不拼进 URL。
服务地址：DAYIPAPER_BASE，默认 https://transport.b1n1.dayipaper.com

两个硬约束（文档原文）：
  1. 轮询间隔不得低于 1 秒，过于频繁会被风控拦 —— 本模块强制夹到 MIN_INTERVAL；
  2. 导出接口**计费**，同一任务最多计一次；产出的下载地址 3 分钟后失效 ——
     拿到地址立刻下载，不要存起来下次用。

命令行：
    python -m core.dayipaper --search 海口经济学院                    # 只查规范，不花钱
    python -m core.dayipaper --file 论文.docx --rule <规范ID> --title 题目 --author 姓名
    python -m core.dayipaper --file 论文.docx --school 海口经济学院 --title 题目 --author 姓名 --no-export
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE = "https://transport.b1n1.dayipaper.com"
PREFIX = "/formatter/pat/open/document/format"
UA = "thesis-format/2.2 (+dayipaper-open-api)"

# 文档硬性要求：间隔 < 1 秒会被风控拦，所以调用方传 0.2 也会被夹到 1 秒
MIN_INTERVAL = 1.0
DEFAULT_INTERVAL = 2.0
DEFAULT_TIMEOUT = 900.0          # 15 分钟：复杂稿子确实要跑几分钟，但卡住要能报错
CALL_TIMEOUT = 60.0              # 单次 HTTP（搜索/上传/发起/轮询）
DOWNLOAD_TIMEOUT = 600.0
MAX_DOWNLOAD_MB = 200

STATUS_DONE = "COMPLETED"
STATUS_FAIL = "ERROR"

# status → 界面文案。未列出的状态直接显示接口给的 statusLabel
STATUS_TEXT = {
    "CREATED": "任务已创建",
    "PREPARE": "准备中",
    "PARSE_DATA": "解析数据中",
    "FORMATTING": "排版中",
    "BUILD_PDF": "正在生成文件",
    "BUILD_PDF_COMPLETED": "预览文档已生成",
    "COMPLETED": "已完成",
    "ERROR": "出错",
}

FORMATS = {1: ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
           2: ("doc", "application/msword")}


class ApiError(RuntimeError):
    """接口失败：code != 0、HTTP 非 2xx、或连不上。

    带上 code / http_status 是为了让上层能区分「PAT 无效（401）」「参数错（400）」
    「服务端炸了（5xx）」，而不是一律当网络故障重试。
    """

    def __init__(self, message, code=None, http_status=None, data=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.data = data


# ---------------------------------------------------------------- 配置读取
# 每次调用都读环境变量，而不是模块导入时快照：测试和 Web 端都能在运行中改配置
def pat():
    value = (os.environ.get("DAYIPAPER_PAT") or "").strip()
    if not value:
        raise ApiError("未配置环境变量 DAYIPAPER_PAT（开放平台申请的 Personal Access Token）")
    return value


def has_token():
    return bool((os.environ.get("DAYIPAPER_PAT") or "").strip())


def base_url():
    return (os.environ.get("DAYIPAPER_BASE") or DEFAULT_BASE).rstrip("/")


def _short(raw, limit=200):
    text = (raw or b"").decode("utf-8", "replace")
    return text[:limit] + ("…" if len(text) > limit else "")


# ---------------------------------------------------------------- 统一信封
def _unwrap(raw, http_status):
    """拆统一信封 {code, message, data}。

    注意：鉴权失败时对方返回的是 **HTTP 401 + 正常信封**（body 里 message=登录状态无效），
    所以不能靠 HTTP 状态判断成败 —— 先解 body，信封里的 code 才是权威。
    """
    body = None
    if raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = None

    if isinstance(body, dict) and "code" in body:
        if body.get("code") == 0:
            return body.get("data")
        raise ApiError(body.get("message") or "接口返回失败",
                       code=body.get("code"), http_status=http_status, data=body.get("data"))

    raise ApiError("接口返回了非预期内容（HTTP %s）：%s" % (http_status, _short(raw)),
                   http_status=http_status)


def _multipart(field, filename, payload, content_type):
    boundary = "----thesis" + uuid.uuid4().hex
    safe = (filename or "document.docx").replace('"', "").replace("\r", "").replace("\n", "")
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, field, safe, content_type)
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return boundary, head + payload + tail


def _request(method, path, query=None, payload=None, files=None, timeout=None):
    url = base_url() + PREFIX + path
    if query:
        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    # 请求头里带上 PAT；注意别把 PAT 拼进查询串，否则会落进对端访问日志
    headers = {"Authorization": "Bearer " + pat(), "Accept": "application/json",
               "User-Agent": UA}
    data = None
    if files is not None:
        field, filename, content, ctype = files
        boundary, data = _multipart(field, filename, content, ctype)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or CALL_TIMEOUT) as resp:
            return _unwrap(resp.read(), resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        _unwrap(raw, exc.code)          # 信封有 code 时这里必定抛 ApiError
        raise ApiError("HTTP %s" % exc.code, http_status=exc.code)
    except urllib.error.URLError as exc:
        raise ApiError("连不上 %s：%s（如需代理请设置 HTTPS_PROXY）"
                       % (base_url(), getattr(exc, "reason", exc)))


# ---------------------------------------------------------------- 六个接口
def search_rules(keywords, page_num=1, page_size=5):
    """1 · 规范搜索。返回 {pageNum, pageSize, total, items[]}，items[].id 就是 ruleId。"""
    if not (keywords or "").strip():
        raise ApiError("搜索关键词不能为空")
    data = _request("GET", "/searchRule",
                    query={"keywords": keywords.strip(), "pageNum": page_num, "pageSize": page_size})
    if not isinstance(data, dict):
        raise ApiError("规范搜索返回结构异常：%s" % (data,))
    return data


def pick_rule(keywords, page_size=5):
    """按学校名挑第一条规范，返回 items[0]；找不到时抛出可读的错误。"""
    found = search_rules(keywords, page_size=page_size)
    items = found.get("items") or []
    if not items:
        raise ApiError("没搜到「%s」的排版规范，请换关键词，或直接用 --rule 传规范 ID" % keywords)
    return items[0]


def upload_document(source, filename=None):
    """2 · 上传源文件。source 可以是路径、bytes 或 file-like。返回 fileUri。"""
    if isinstance(source, (bytes, bytearray)):
        payload = bytes(source)
        name = filename or "document.docx"
    elif hasattr(source, "read"):
        payload = source.read()
        name = filename or getattr(source, "name", "document.docx")
        name = Path(str(name)).name
    else:
        path = Path(source)
        if not path.is_file():
            raise ApiError("源文件不存在：%s" % path)
        payload = path.read_bytes()
        name = filename or path.name
    if not payload:
        raise ApiError("源文件是空的：%s" % (filename or "bytes"))

    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    data = _request("POST", "/uploadRowDocument",
                    files=("file", name, payload, ctype), timeout=180)
    if not isinstance(data, str) or not data:
        raise ApiError("上传返回结构异常，未拿到 fileUri：%s" % (data,))
    return data


def request_format(file_uri, rule_id, title, author_name, extra=None):
    """3 · 发起排版。返回 taskId。title / authorName / fileUri / ruleId 是必填。"""
    body = {"title": title, "authorName": author_name,
            "fileUri": file_uri, "ruleId": rule_id}
    for key, value in (extra or {}).items():
        if value not in (None, ""):
            body[key] = value
    missing = [k for k in ("title", "authorName", "fileUri", "ruleId") if not body.get(k)]
    if missing:
        raise ApiError("发起排版缺少必填参数：%s" % "、".join(missing))

    data = _request("POST", "/requestDocumentFormat", payload=body)
    if not isinstance(data, str) or not data:
        raise ApiError("发起排版返回结构异常，未拿到任务 ID：%s" % (data,))
    return data


def trace(task_id):
    """4 · 追踪任务状态。返回状态字典（含 status / statusLabel / pdfShareUrl / errorReason）。"""
    if not task_id:
        raise ApiError("taskId 不能为空")
    data = _request("GET", "/traceTask", query={"taskId": task_id})
    if not isinstance(data, dict):
        raise ApiError("任务状态返回结构异常：%s" % (data,))
    return data


def suggestions(task_id):
    """5 · 排版建议提示。结构不清晰时给出「哪一行像标题但不确定」的提示，没有则返回 []。"""
    data = _request("GET", "/formatSuggestions", query={"taskId": task_id})
    if data is None:
        return []
    if not isinstance(data, list):
        raise ApiError("建议提示返回结构异常：%s" % (data,))
    return data


def export_url(task_id, fmt=1):
    """6 · 导出文档（**计费**）。返回下载地址，3 分钟内有效，拿到就下。"""
    if fmt not in FORMATS:
        raise ApiError("format 只能是 1（docx）或 2（doc）")
    data = _request("GET", "/downloadFormattedDocument",
                    query={"taskId": task_id, "format": fmt})
    if not isinstance(data, str) or not data.startswith("http"):
        raise ApiError("导出返回结构异常，未拿到下载地址：%s" % (data,))
    return data


def download_to(task_id, dest, fmt=1, timeout=DOWNLOAD_TIMEOUT):
    """导出并下载到本地。返回落盘路径。

    导出地址是 OSS 预签名链接：**不要**带 Authorization 头（多余的鉴权头会被拒绝），
    也不能缓存（3 分钟失效）——所以这里每次都现取现下。
    """
    url = export_url(task_id, fmt)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    size = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_DOWNLOAD_MB * 1024 * 1024:
                    raise ApiError("产物超过 %d MB，已中止下载" % MAX_DOWNLOAD_MB)
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise ApiError("下载产物失败：HTTP %s（链接 3 分钟失效，重试会重新取地址）" % exc.code,
                       http_status=exc.code)
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise ApiError("下载产物失败：%s" % getattr(exc, "reason", exc))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    if size == 0:
        part.unlink(missing_ok=True)
        raise ApiError("下载到 0 字节，导出地址可能已失效")
    part.replace(dest)
    return str(dest)


# ---------------------------------------------------------------- 编排
def wait(task_id, interval=DEFAULT_INTERVAL, timeout=DEFAULT_TIMEOUT, on_progress=None):
    """轮询到 COMPLETED 为止，返回最后一次状态字典。

    interval 会被夹到 >= MIN_INTERVAL（文档要求）；超时抛错而不是无限挂住。
    """
    interval = max(float(interval or 0), MIN_INTERVAL)
    started = time.time()
    last = None
    while True:
        info = trace(task_id)
        status = info.get("status") or ""
        if on_progress:
            on_progress(info)
        if status == STATUS_DONE:
            return info
        if status == STATUS_FAIL:
            raise ApiError("排版失败：%s" % (info.get("errorReason") or "接口未给出原因"),
                           data=info)
        last = info
        waited = time.time() - started
        if waited > float(timeout or DEFAULT_TIMEOUT):
            raise ApiError("排版超时（%d 秒），任务 %s 仍停留在「%s」"
                           % (waited, task_id, status or "未知"),
                           data=last)
        time.sleep(interval)


def format_document(source, rule_id=None, school=None, title=None, author_name=None,
                    out_dir=None, fmt=1, interval=DEFAULT_INTERVAL, timeout=DEFAULT_TIMEOUT,
                    on_progress=None, on_log=None, export=True, extra=None):
    """一条命令走完 6 步。返回结果字典。

    export=False 时停在 COMPLETED（不计费），只拿预览地址；确认要成品再导出。
    """
    def log(*args):
        if on_log:
            on_log(" ".join(str(a) for a in args))

    if not rule_id:
        if not school:
            raise ApiError("必须给出 rule_id，或给 school 让本模块自动搜第一条规范")
        rule = pick_rule(school)
        rule_id = rule.get("id")
        log("规范：%s（%s）" % (rule.get("name"), rule_id))

    if not title or not author_name:
        raise ApiError("title 与 author_name 是接口必填项")

    if isinstance(source, (bytes, bytearray)) or hasattr(source, "read"):
        stem = "thesis"
    else:
        stem = Path(source).stem

    log("上传源文档…")
    file_uri = upload_document(source)
    log("  fileUri = %s" % file_uri)

    log("发起排版…")
    task_id = request_format(file_uri, rule_id, title, author_name, extra=extra)
    log("  taskId  = %s" % task_id)

    log("轮询任务状态（间隔 %gs，上限 %gs）…" % (max(interval, MIN_INTERVAL), timeout))
    info = wait(task_id, interval=interval, timeout=timeout, on_progress=on_progress)
    log("  状态：%s %s" % (info.get("status"), info.get("statusLabel") or ""))
    if info.get("openDocumentError"):
        log("  注意：源文档无法打开（openDocumentError）")
    if info.get("cantDetectedComponent"):
        log("  注意：没有检测到任何有效组件（cantDetectedComponent）")

    result = {"taskId": task_id, "ruleId": rule_id, "ruleName": info.get("ruleName"),
              "status": info.get("status"), "trace": info, "suggestions": [],
              "file": None, "previewUrl": info.get("pdfShareUrl")}

    try:
        result["suggestions"] = suggestions(task_id)
        if result["suggestions"]:
            log("  有 %d 条排版建议，建议先看一眼再导出" % len(result["suggestions"]))
    except ApiError as exc:
        log("  建议提示获取失败（不影响成品）：%s" % exc)

    if not export:
        log("按 export=False 停在预览阶段：导出接口计费，确认后再导出")
        return result

    suffix = FORMATS[fmt][0]
    out_path = Path(out_dir or ".") / ("%s-%s.%s" % (stem, task_id, suffix))
    log("导出 %s（计费）→ %s" % (suffix, out_path))
    result["file"] = download_to(task_id, out_path, fmt=fmt)
    return result


# ---------------------------------------------------------------- 命令行
def _print_rules(keywords, page_size=5):
    found = search_rules(keywords, page_size=page_size)
    items = found.get("items") or []
    print("「%s」共 %s 条规范：" % (keywords, found.get("total", len(items))))
    for item in items:
        print("  %-26s %s" % (item.get("id"), item.get("name")))
        brief = (item.get("brief") or "").strip()
        if brief:
            print("      %s" % brief[:120])
        print("      学校=%s 层次=%s 类型=%s 使用次数=%s 所属=%s"
              % (item.get("targetSchoolName"), item.get("targetSubjectLevel"),
                 item.get("targetSubjectType"), item.get("usedCount"), item.get("ownerType")))
    return items


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m core.dayipaper",
        description="大易论文排版开放接口：查规范 / 云端排版 / 导出成品")
    ap.add_argument("--search", metavar="关键词", help="只搜规范（打印 ruleId，不发起排版、不计费）")
    ap.add_argument("--file", help="待排版的源文档（.docx / .doc）")
    ap.add_argument("--rule", help="规范 ID（searchRule 返回的 id）")
    ap.add_argument("--school", help="学校关键词；不给 --rule 时用它的第一条规范")
    ap.add_argument("--title", help="中文标题（必填）")
    ap.add_argument("--author", help="作者中文名（必填）")
    ap.add_argument("--tutor", help="导师中文名")
    ap.add_argument("--subject", help="专业班级")
    ap.add_argument("--title-en", help="英文标题")
    ap.add_argument("--author-en", help="作者英文名")
    ap.add_argument("--tutor-en", help="导师英文名")
    ap.add_argument("--out", help="产物目录（默认当前目录）")
    ap.add_argument("--fmt", type=int, default=1, choices=[1, 2], help="1=docx 2=doc（默认 1）")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="轮询间隔秒，最低 1")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="轮询超时秒")
    ap.add_argument("--no-export", action="store_true", help="只到预览，不导出（不产生费用）")
    args = ap.parse_args(argv)

    def log(*a):
        print(" ".join(str(x) for x in a), flush=True)

    try:
        if args.search:
            _print_rules(args.search)
            return 0
        if not args.file:
            ap.error("要么给 --search 查规范，要么给 --file 排版")

        def on_progress(info):
            label = info.get("statusLabel") or STATUS_TEXT.get(info.get("status"), "")
            log("  %s %s" % (info.get("status"), label))

        result = format_document(
            args.file, rule_id=args.rule, school=args.school,
            title=args.title, author_name=args.author, out_dir=args.out, fmt=args.fmt,
            interval=args.interval, timeout=args.timeout, export=not args.no_export,
            on_progress=on_progress, on_log=log,
            extra={"tutorName": args.tutor, "subjectName": args.subject,
                   "titleEn": args.title_en, "authorNameEn": args.author_en,
                   "tutorNameEn": args.tutor_en})
    except ApiError as exc:
        sys.stderr.write("失败：%s\n" % exc)
        return 2

    print("任务 %s 已完成（规范：%s）" % (result["taskId"], result.get("ruleName") or result["ruleId"]))
    if result.get("suggestions"):
        print("排版建议：")
        for item in result["suggestions"]:
            print("  · %s" % item.get("message"))
    if result.get("previewUrl"):
        print("预览（3 分钟内有效）：%s" % result["previewUrl"])
    if result.get("file"):
        print("成品：%s" % result["file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
