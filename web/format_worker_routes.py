"""【阿里云版】海外排版服务器的内部接口：只给主站调用，用户碰不到。

这台服务器装着 LibreOffice，用现有的 `_run_school` 排版，结果留在本机；
主站通过下面几个接口提交原稿、轮询进度、把 docx / pdf 拷走（见 core/format_remote.py）。

开关：设了 FORMAT_WORKER_TOKEN（或 _FILE）且本机有 LibreOffice（THESIS_LOCAL_FORMAT=true）才开放；
没开时这些路径一律 404，不暴露它们存在。鉴权失败 401。密钥用恒定时间比较。
建议在 Caddy 里再按主站的 IP 限制一层，见 docs/aliyun-split.md。
"""
import hashlib
import hmac
import json
import os
import threading
import time

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core import format_remote

router = APIRouter(prefix=format_remote.PREFIX)

_ctx = {}
_idem = {}                     # 幂等键 → (任务号, 时间)：主站重传同一份原稿时不会排两遍
_lock = threading.Lock()
IDEM_TTL = 3600
META_KEYS = ("title", "authorName", "titleEn", "subTitle", "subTitleEn", "authorNameEn",
             "tutorName", "tutorNameEn", "subjectName", "subjectNameEn")


def bind(**kwargs):
    """由 web/app.py 注入：jobs / new_job / run / running / limit / drop / max_bytes / engine。"""
    _ctx.update(kwargs)


def _guard(authorization):
    expected = format_remote.token()
    if not expected or not _ctx["engine"]():
        raise HTTPException(404, "Not Found")
    given = authorization[7:] if (authorization or "").lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "密钥不对")


def _job(job_id):
    job = _ctx["jobs"].get(job_id)
    if not job or job.get("kind") != "school":
        raise HTTPException(404, "排版任务不存在（排版服务器重启会丢掉进行中的任务）")
    return job


@router.get("/health")
def health(authorization: str | None = Header(None)):
    _guard(authorization)
    return {"ok": True, "running": _ctx["running"](), "capacity": _ctx["limit"]()}


@router.post("/jobs")
async def create_job(file: UploadFile = File(...), meta: str = Form(...),
                     authorization: str | None = Header(None),
                     idempotency_key: str | None = Header(None)):
    _guard(authorization)
    try:
        info = json.loads(meta)
    except ValueError:
        raise HTTPException(400, "meta 不是有效 JSON") from None
    if not isinstance(info, dict) or not info.get("title") or not info.get("authorName"):
        raise HTTPException(400, "题目和作者名是必填")
    suffix = info.get("sourceSuffix", ".docx")
    if suffix not in (".docx", ".doc"):
        raise HTTPException(400, "只收 .docx / .doc 源文件")
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传的文件是空的")
    if len(data) > _ctx["max_bytes"]():
        raise HTTPException(413, "文件太大")
    # 只收白名单里的字段；规范固定是海口经济学院，不听主站传的 ruleId
    clean = {key: str(info[key]).strip() for key in META_KEYS if str(info.get(key) or "").strip()}
    clean.update(ruleId="school:hkjjxy", sourceSuffix=suffix)
    if isinstance(info.get("overrides"), list) and info["overrides"]:
        clean["overrides"] = info["overrides"]

    key = (idempotency_key or "").strip()[:80]
    with _lock:
        now = time.time()
        for stale in [k for k, (_, at) in _idem.items() if now - at > IDEM_TTL]:
            del _idem[stale]
        if key and key in _idem and _ctx["jobs"].get(_idem[key][0]):
            return {"id": _idem[key][0], "reused": True}
        if _ctx["running"]() >= _ctx["limit"]():
            raise HTTPException(429, "当前排版任务较多，请稍后再提交")
        job = _ctx["new_job"](kind="school")
        job.update({"name": clean["title"], "school_key": "school:hkjjxy", "stage": "准备本地排版",
                    "status": None, "statusLabel": "", "taskId": None, "suggestions": [],
                    "preview": None, "exported": False})
        if key:
            _idem[key] = (job["id"], now)
    threading.Thread(target=_ctx["run"], args=(job, data, clean), daemon=True).start()
    return {"id": job["id"], "reused": False}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, authorization: str | None = Header(None)):
    _guard(authorization)
    job = _job(job_id)
    return {"id": job["id"], "state": job["state"], "stage": job.get("stage"),
            "log": job["log"][-80:], "logCount": len(job["log"]),
            "suggestions": job.get("suggestions") or [],
            "files": {kind: os.path.getsize(path) for kind, path in job["files"].items()
                      if os.path.isfile(path)}}


@router.get("/jobs/{job_id}/files/{kind}")
def job_file(job_id: str, kind: str, authorization: str | None = Header(None)):
    _guard(authorization)
    job = _job(job_id)
    path = job["files"].get(kind) if kind in ("docx", "pdf") else None
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "没有这个文件")
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return FileResponse(path, headers={"X-Content-SHA256": digest.hexdigest(), "Cache-Control": "no-store"})


@router.delete("/jobs/{job_id}")
def job_drop(job_id: str, authorization: str | None = Header(None)):
    _guard(authorization)
    job = _ctx["jobs"].get(job_id)
    if job and job.get("kind") == "school":
        _ctx["drop"](job)
    return {"ok": True}
