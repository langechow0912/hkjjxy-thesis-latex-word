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
from core import docx2tex, profile as P      # noqa: E402

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse
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
            "running": sum(1 for j in JOBS.values() if j["state"] == "running")}


def _running_count():
    return sum(1 for j in JOBS.values() if j["state"] == "running")


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


def _new_job():
    with _LOCK:
        jid = "j%03d" % (len(JOBS) + 1)
        job = {"id": jid, "state": "running", "log": [], "files": {},
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
    if not job:
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
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
