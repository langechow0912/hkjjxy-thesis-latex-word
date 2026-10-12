# -*- coding: utf-8 -*-
"""毕业论文格式流水线 · 本地 Web 界面。

启动：
    python web/app.py                       # 默认 http://127.0.0.1:8000
    python web/app.py --port 8080
    python -m uvicorn web.app:app --port 8000

接口：
    GET  /api/schools                       学校列表
    POST /api/convert   (file, school)      docx → tex（返回 tex 文本供编辑）
    POST /api/build     {school,name,tex}   提交构建，返回 job id
    GET  /api/job/{id}                      任务状态与日志
    GET  /api/download/{id}/{pdf|docx}      下载产物
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
import threading
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
except ImportError:                                       # pragma: no cover
    sys.stderr.write("缺少依赖，请先执行：pip install -r requirements.txt\n")
    raise

app = FastAPI(title="毕业论文格式流水线")
STATIC = Path(__file__).resolve().parent / "static"

JOBS = {}
_LOCK = threading.Lock()

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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
               "tmp": tempfile.mkdtemp(prefix="thesis-web-")}
        JOBS[jid] = job
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
    cfg = P.load(req.school)
    name = sanitize(req.name)
    work = Path(P.SAMPLES) / req.school / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "main.tex").write_text(req.tex, encoding="utf-8", newline="\n")

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
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
