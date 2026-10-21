# -*- coding: utf-8 -*-
"""毕业论文格式流水线 · 本地 Web 界面。

启动：
    python web/app.py                       # 默认 http://127.0.0.1:8000
    python web/app.py --port 8080
    python -m uvicorn web.app:app --port 8000

公网暴露前建议设置（不设置则保持本地免密）：
    THESIS_AUTH_USER / THESIS_AUTH_PASS     开启 HTTP Basic 访问口令
    THESIS_MAX_JOBS                         同时编译的任务上限（默认 2）
    THESIS_MAX_UPLOAD_MB                    上传 docx 大小上限（默认 20）
    THESIS_KEEP_JOBS                        内存中保留的任务数（默认 50）

接口：
    GET  /api/schools                       学校列表
    POST /api/convert   (file, school)      docx → tex（返回 tex 文本供编辑）
    POST /api/build     {school,name,tex}   提交构建，返回 job id
    GET  /api/job/{id}                      任务状态与日志
    GET  /api/download/{id}/{pdf|docx}      下载产物

云端排版通道（大易论文排版开放接口，见 core/dayipaper.py 与 docs/dayipaper-api.md）：
    GET  /api/dayipaper                     通道是否可用（有没有配 PAT）
    GET  /api/dayipaper/rules?q=&size=      规范搜索，返回 ruleId 列表
    POST /api/dayipaper/format              上传 .docx + 规范 + 题目信息，返回 job id
    GET  /api/dayipaper/job/{id}            云端任务状态、进度文案、排版建议
    GET  /api/dayipaper/preview/{id}        跳转到刚取的预览 PDF 地址（原地址 3 分钟失效）
    GET  /api/dayipaper/download/{id}       导出成品（**计费**，必须带 confirm=true）
"""
import argparse
import base64
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import build as corebuild          # noqa: E402
from core import dayipaper                   # noqa: E402
from core import docx2tex, profile as P      # noqa: E402

try:
    from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    from pydantic import BaseModel
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
except ImportError:                                       # pragma: no cover
    sys.stderr.write("缺少依赖，请先执行：pip install -r requirements.txt\n")
    raise

app = FastAPI(title="毕业论文格式流水线")
STATIC = Path(__file__).resolve().parent / "static"

JOBS = {}
_LOCK = threading.Lock()

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# ---- 公网暴露时的保护措施（全部可关，默认本地免密）----
# 设置这两个环境变量即开启 HTTP Basic 口令；留空则任何人可访问。
AUTH_USER = os.environ.get("THESIS_AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("THESIS_AUTH_PASS", "").strip()
AUTH_ON = bool(AUTH_USER and AUTH_PASS)
# 同时编译的任务数上限：一次构建要跑 3 遍 XeLaTeX + LibreOffice，很吃 CPU。
MAX_RUNNING = int(os.environ.get("THESIS_MAX_JOBS", "2") or 2)
MAX_UPLOAD_MB = int(os.environ.get("THESIS_MAX_UPLOAD_MB", "20") or 20)
KEEP_JOBS = int(os.environ.get("THESIS_KEEP_JOBS", "50") or 50)

# ---- 云端排版通道（大易论文排版开放接口）----
# PAT 只从环境变量 DAYIPAPER_PAT 读（core/dayipaper.py 每次调用现取），没配就整条通道不可用。
# 云端任务不占 CPU（只是长轮询），所以并发上限和本地编译的 MAX_RUNNING 分开算，
# 但它们同样受 KEEP_JOBS 回收策略管理。
MAX_CLOUD = int(os.environ.get("THESIS_MAX_CLOUD_JOBS", "3") or 3)
# 轮询间隔对方有硬性下限：低于 1 秒会被风控拦，core/dayipaper.py 里还会再夹一次。
CLOUD_POLL = float(os.environ.get("THESIS_CLOUD_POLL", "2") or 2)
CLOUD_TIMEOUT = float(os.environ.get("THESIS_CLOUD_TIMEOUT", "900") or 900)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def _denied(self):
        return Response(
            content="需要登录", status_code=401, media_type="text/plain; charset=utf-8",
            headers={"WWW-Authenticate": 'Basic realm="thesis", charset="UTF-8"'},
        )

    async def dispatch(self, request, call_next):
        if not AUTH_ON or request.url.path == "/api/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header[:6].lower() == "basic ":
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, pwd = raw.partition(":")
                ok = (secrets.compare_digest(user, AUTH_USER)
                      and secrets.compare_digest(pwd, AUTH_PASS))
            except Exception:
                ok = False
        if not ok:
            return self._denied()
        return await call_next(request)


app.add_middleware(BasicAuthMiddleware)


@app.get("/api/health")
def health():
    return {"ok": True, "auth": AUTH_ON,
            "running": _running_count(),
            "cloud": sum(1 for j in JOBS.values()
                         if j.get("kind") == "cloud" and j["state"] == "running")}


def _running_count():
    """只数本地编译任务：云端排版只是长轮询，不该挤占 XeLaTeX 的并发额度。"""
    return sum(1 for j in JOBS.values() if j["state"] == "running" and j.get("kind") != "cloud")


def _cloud_running_count():
    return sum(1 for j in JOBS.values() if j["state"] == "running" and j.get("kind") == "cloud")


# 最近一次上传转换落地的目录（进程内），用于论文改名后把图片补回来
_LAST_CONVERT = {}

_RE_FIG = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")


def _prepare_figures(work, school, tex, log):
    """保证 tex 引用的图片真实存在；返回仍然缺失的图片路径列表。"""
    refs = _RE_FIG.findall(tex or "")
    refs = [r.strip() for r in refs if r.strip()]
    if not refs:
        return []
    missing = [r for r in refs if not (work / r).is_file()]
    if not missing:
        return []
    src = _LAST_CONVERT.get(school)
    if src and (Path(src) / "figures").is_dir():
        shutil.copytree(Path(src) / "figures", work / "figures", dirs_exist_ok=True)
        log("  图片目录缺失，已从上次转换目录补回：%s" % (work / "figures"))
    return [r for r in refs if not (work / r).is_file()]


def _reap_jobs():
    """只保留最近 KEEP_JOBS 个任务，顺手清掉临时目录，避免长时间在线后堆满磁盘。"""
    if len(JOBS) <= KEEP_JOBS:
        return
    oldest = sorted(JOBS.values(), key=lambda j: j.get("t", 0))[:len(JOBS) - KEEP_JOBS]
    for job in oldest:
        JOBS.pop(job["id"], None)
        shutil.rmtree(job.get("tmp", ""), ignore_errors=True)


def sanitize(name, fallback="thesis"):
    name = _BAD.sub("", (name or "").strip())
    name = name.strip(". ") or fallback
    return name[:60]


class BuildReq(BaseModel):
    school: str
    name: str
    tex: str
    pdf: bool = True
    docx: bool = True


def _new_job(kind="build"):
    with _LOCK:
        jid = "j%03d" % (len(JOBS) + 1)
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "files": {},
               "tmp": tempfile.mkdtemp(prefix="thesis-web-"), "t": time.time()}
        JOBS[jid] = job
    _reap_jobs()
    return job


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/schools")
def schools():
    out = []
    if not os.path.isdir(P.SCHOOLS):
        return out
    for d in sorted(Path(P.SCHOOLS).iterdir()):
        if (d / "profile.yaml").is_file():
            try:
                cfg = P.load(d.name)
                out.append({"id": d.name,
                            "name": cfg["school"].get("name", d.name)})
            except Exception:
                pass
    return out


@app.post("/api/convert")
async def api_convert(file: UploadFile = File(...), school: str = Form("hkjjxy")):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".docx":
        raise HTTPException(400, "目前只支持 .docx；若已有 main.tex，请用「直接粘贴 tex」")
    name = sanitize(Path(file.filename).stem)
    work = Path(P.SAMPLES) / school / name
    work.mkdir(parents=True, exist_ok=True)
    src = work / "_source.docx"
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, "文件超过 %d MB，请压缩图片后重试" % MAX_UPLOAD_MB)
    src.write_bytes(data)

    cfg = P.load(school)
    lines = []

    def log(*a):
        lines.append(" ".join(str(x) for x in a))

    try:
        tex, stat = docx2tex.convert(str(src), str(work), cfg, log=log)
    except Exception:
        raise HTTPException(500, "转换失败：\n" + traceback.format_exc())

    (work / "main.tex").write_text(tex, encoding="utf-8", newline="\n")
    _LAST_CONVERT[school] = str(work)
    return {"name": name, "school": school, "tex": tex,
            "stat": stat, "log": lines}


def _run(job, cfg, name, tex, do_pdf, do_docx):
    def log(*a):
        job["log"].append(" ".join(str(x) for x in a))
    try:
        paths = corebuild.build_one(cfg, name, tmp=job["tmp"],
                                    do_docx=do_docx, do_pdf=do_pdf,
                            log=log)
        files = {}
        pdf_path = Path(paths["work"]) / "main.pdf"
        if pdf_path.is_file():
            files["pdf"] = str(pdf_path)
        docx_path = paths["docx"]
        if os.path.isfile(docx_path):
            files["docx"] = docx_path
        job["files"] = files
        job["state"] = "done"
    except BaseException:
        job["state"] = "error"
        job["log"].append(traceback.format_exc())


@app.post("/api/build")
def api_build(req: BuildReq):
    if _running_count() >= MAX_RUNNING:
        raise HTTPException(503, "已有 %d 个任务在编译，请等它们完成后再提交（本机同时最多 %d 个）"
                            % (_running_count(), MAX_RUNNING))
    cfg = P.load(req.school)
    name = sanitize(req.name)
    work = Path(P.SAMPLES) / req.school / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "main.tex").write_text(req.tex, encoding="utf-8", newline="\n")

    # 缺图会编译出「看起来成功但没有图」的 PDF，这里拦在构建之前
    missing = _prepare_figures(work, req.school, req.tex, print)
    if missing:
        raise HTTPException(400, "缺少 %d 个图片文件（例如 %s）。论文改名或换目录后图片不会跟着走，"
                                 "请重新上传一次 Word 稿，或把 figures 目录放回论文目录下。"
                            % (len(missing), "、".join(missing[:5])))

    job = _new_job()
    job["name"] = name
    t = threading.Thread(target=_run, args=(job, cfg, name, req.tex,
                                            req.pdf, req.docx), daemon=True)
    t.start()
    return {"job": job["id"], "name": name}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("kind") == "cloud":
        raise HTTPException(404, "任务不存在")
    return {"id": job["id"], "state": job["state"], "log": job["log"],
            "files": {k: os.path.basename(v) for k, v in job["files"].items()}}


@app.get("/api/download/{job_id}/{kind}")
def api_download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job or kind not in job.get("files", {}):
        raise HTTPException(404, "产物不存在")
    path = job["files"][kind]
    return FileResponse(path, filename=os.path.basename(path))


# ---------------------------------------------------------------- 云端排版通道
# 复用 JOBS（kind="cloud"）：本地编译要跑 3 遍 XeLaTeX，云端只是长轮询，
# 所以并发额度和 _running_count 分开算，避免云端任务把本地编译挡在门外。
def _dp_call(fn, *args, **kwargs):
    """把接口异常翻成 HTTP 错误：鉴权类原样透出，其余归为 502。"""
    try:
        return fn(*args, **kwargs)
    except dayipaper.ApiError as exc:
        status = exc.http_status if exc.http_status in (400, 401, 403, 413, 429) else 502
        raise HTTPException(status, str(exc))


def _cloud_job(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("kind") != "cloud":
        raise HTTPException(404, "云端排版任务不存在")
    return job


@app.get("/api/dayipaper")
def api_dp_info():
    return {"enabled": dayipaper.has_token(), "base": dayipaper.base_url(),
            "maxJobs": MAX_CLOUD, "poll": max(CLOUD_POLL, dayipaper.MIN_INTERVAL),
            "timeout": CLOUD_TIMEOUT, "running": _cloud_running_count()}


@app.get("/api/dayipaper/rules")
def api_dp_rules(q: str = Query(..., min_length=1, description="学校名或关键词"),
                 size: int = Query(5, ge=1, le=20)):
    if not dayipaper.has_token():
        raise HTTPException(503, "云端排版未启用：请设置环境变量 DAYIPAPER_PAT 后重启服务")
    return _dp_call(dayipaper.search_rules, q, page_size=size)


@app.post("/api/dayipaper/format")
async def api_dp_format(file: UploadFile = File(...),
                        ruleId: str = Form(...),
                        title: str = Form(...),
                        authorName: str = Form(...),
                        titleEn: str = Form(""),
                        subTitle: str = Form(""),
                        subTitleEn: str = Form(""),
                        authorNameEn: str = Form(""),
                        tutorName: str = Form(""),
                        tutorNameEn: str = Form(""),
                        subjectName: str = Form(""),
                        subjectNameEn: str = Form("")):
    if not dayipaper.has_token():
        raise HTTPException(503, "云端排版未启用：请设置环境变量 DAYIPAPER_PAT 后重启服务")
    if _cloud_running_count() >= MAX_CLOUD:
        raise HTTPException(503, "已有 %d 个云端任务在跑，请稍后再提交（上限 %d）"
                            % (_cloud_running_count(), MAX_CLOUD))

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".docx", ".doc"):
        raise HTTPException(400, "云端排版只收 .docx / .doc 源文件")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传的文件是空的")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, "文件超过 %d MB，请压缩图片后重试" % MAX_UPLOAD_MB)

    meta = {"title": title.strip(), "authorName": authorName.strip(), "ruleId": ruleId.strip()}
    for key, value in (("titleEn", titleEn), ("subTitle", subTitle), ("subTitleEn", subTitleEn),
                       ("authorNameEn", authorNameEn), ("tutorName", tutorName),
                       ("tutorNameEn", tutorNameEn), ("subjectName", subjectName),
                       ("subjectNameEn", subjectNameEn)):
        if (value or "").strip():
            meta[key] = value.strip()
    if not all(meta.get(k) for k in ("title", "authorName", "ruleId")):
        raise HTTPException(400, "题目、作者名、规范 ID 都是必填")

    job = _new_job(kind="cloud")
    job.update({"name": meta["title"], "stage": "上传源文档", "status": None,
                "statusLabel": "", "taskId": None, "suggestions": [],
                "preview": None, "exported": False})
    threading.Thread(target=_run_cloud,
                     args=(job, data, file.filename or "source.docx", meta),
                     daemon=True).start()
    return {"job": job["id"], "title": meta["title"]}


def _run_cloud(job, payload, filename, meta):
    def log(*a):
        job["log"].append(" ".join(str(x) for x in a))

    extra = {k: v for k, v in meta.items() if k not in ("title", "authorName", "ruleId")}
    try:
        file_uri = dayipaper.upload_document(payload, filename)
        log("上传完成：fileUri = %s" % file_uri)

        job["stage"] = "发起排版"
        task_id = dayipaper.request_format(file_uri, meta["ruleId"], meta["title"],
                                          meta["authorName"], extra=extra)
        job["taskId"] = task_id
        log("任务已创建：taskId = %s" % task_id)

        last = {"status": None}

        def tick(info):
            status = info.get("status") or ""
            job["status"] = status
            job["statusLabel"] = info.get("statusLabel") or dayipaper.STATUS_TEXT.get(status, "")
            job["preview"] = info.get("pdfShareUrl") or job.get("preview")
            if status != last["status"]:          # 只在状态变化时写日志，别把轮询刷满
                last["status"] = status
                log("%s %s" % (status, job["statusLabel"]))

        job["stage"] = "云端排版中"
        job["trace"] = dayipaper.wait(task_id, interval=CLOUD_POLL,
                                      timeout=CLOUD_TIMEOUT, on_progress=tick)
        try:
            job["suggestions"] = dayipaper.suggestions(task_id)
        except dayipaper.ApiError as exc:
            log("建议提示获取失败（不影响成品）：%s" % exc)
        if job["suggestions"]:
            log("有 %d 条排版建议，导出前建议先看一眼" % len(job["suggestions"]))
        job["stage"] = "排版完成（尚未导出；导出接口计费）"
        job["state"] = "done"
    except dayipaper.ApiError as exc:
        job["state"] = "error"
        job["stage"] = "失败"
        log("接口报错：%s" % exc)
    except BaseException:
        job["state"] = "error"
        job["stage"] = "失败"
        job["log"].append(traceback.format_exc())


@app.get("/api/dayipaper/job/{job_id}")
def api_dp_job(job_id: str):
    job = _cloud_job(job_id)
    return {"id": job["id"], "state": job["state"], "stage": job.get("stage"),
            "status": job.get("status"), "statusLabel": job.get("statusLabel"),
            "taskId": job.get("taskId"), "log": job["log"],
            "suggestions": job.get("suggestions") or [],
            "preview": bool(job.get("preview")), "exported": bool(job.get("exported")),
            "files": {k: os.path.basename(v) for k, v in job.get("files", {}).items()}}


@app.get("/api/dayipaper/preview/{job_id}")
def api_dp_preview(job_id: str):
    """现取一次状态再跳转：接口给的 pdfShareUrl 只有 3 分钟有效期，存下来会点开失败。"""
    job = _cloud_job(job_id)
    if not job.get("taskId"):
        raise HTTPException(409, "任务还没创建成功")
    if not dayipaper.has_token():
        raise HTTPException(503, "云端排版未启用：请设置环境变量 DAYIPAPER_PAT 后重启服务")
    info = _dp_call(dayipaper.trace, job["taskId"])
    url = info.get("pdfShareUrl")
    if not url:
        raise HTTPException(409, "还没有预览文件（当前状态：%s）"
                            % (info.get("statusLabel") or info.get("status")))
    job["preview"] = url
    return RedirectResponse(url, status_code=303)


@app.get("/api/dayipaper/download/{job_id}")
def api_dp_download(job_id: str, fmt: int = Query(1, ge=1, le=2),
                    confirm: bool = Query(False, description="导出计费，必须显式确认")):
    job = _cloud_job(job_id)
    if not job.get("taskId"):
        raise HTTPException(409, "任务还没创建成功")
    if job["state"] != "done":
        raise HTTPException(409, "任务尚未完成（当前：%s）" % (job.get("stage") or job["state"]))

    kind = dayipaper.FORMATS[fmt][0]
    cached = job.get("files", {}).get(kind)
    if cached and os.path.isfile(cached):        # 同一任务别重复触发导出，省一次计费风险
        return FileResponse(cached, filename=os.path.basename(cached))
    if not confirm:
        raise HTTPException(400, "导出接口按次计费（同一任务最多计一次）。确认导出请带 confirm=true")

    dest = Path(job["tmp"]) / ("%s-%s.%s" % (sanitize(job.get("name") or "thesis"),
                                             job["taskId"], kind))
    try:
        path = dayipaper.download_to(job["taskId"], dest, fmt=fmt)
    except dayipaper.ApiError as exc:
        job["log"].append("导出失败：%s" % exc)
        raise HTTPException(exc.http_status or 502, str(exc))
    job["files"][kind] = path
    job["exported"] = True
    job["log"].append("已导出成品：%s" % os.path.basename(path))
    return FileResponse(path, filename=os.path.basename(path))


def main(argv=None):
    ap = argparse.ArgumentParser(description="毕业论文格式流水线 Web 界面")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    import uvicorn
    print("打开 http://%s:%d" % (a.host, a.port))
    print("访问口令：%s" % ("已开启（用户 %s）" % AUTH_USER if AUTH_ON
                            else "未开启，任何人都能访问"))
    print("同时编译上限：%d 个；上传上限：%d MB" % (MAX_RUNNING, MAX_UPLOAD_MB))
    print("云端排版：%s" % ("已开启 → %s" % dayipaper.base_url() if dayipaper.has_token()
                            else "未开启（设置 DAYIPAPER_PAT 后启用）"))
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
