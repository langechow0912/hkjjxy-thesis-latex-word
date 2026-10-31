# -*- coding: utf-8 -*-
"""毕业论文格式流水线 · API Web 界面。

启动：
    python web/app.py                       # 默认 http://127.0.0.1:8000
    python web/app.py --port 8080
    python -m uvicorn web.app:app --port 8000

公网暴露前建议设置（不设置则保持本地免密）：
    THESIS_AUTH_USER / THESIS_AUTH_PASS(_FILE) 开启 HTTP Basic 访问口令
    THESIS_MAX_JOBS                         同时编译的任务上限（默认 2）
    THESIS_MAX_UPLOAD_MB                    上传 docx 大小上限（默认 20）
    THESIS_KEEP_JOBS                        内存中保留的任务数（默认 50）

所有院校走云端 API；旧 /api/schools、/api/convert、/api/build 返回 410。

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
import csv
import io
import ipaddress
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import affiliates, alipay, dayipaper   # noqa: E402
from core.localconfig import load_pat
load_pat(ROOT)

try:
    from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
    from pydantic import BaseModel
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import Response
except ImportError:                                       # pragma: no cover
    sys.stderr.write("缺少依赖，请先执行：pip install -r requirements.txt\n")
    raise

PRODUCTION = os.environ.get("THESIS_ENV", "").lower() == "production"
app = FastAPI(title="毕业论文格式流水线",
              docs_url=None if PRODUCTION else "/docs",
              redoc_url=None if PRODUCTION else "/redoc",
              openapi_url=None if PRODUCTION else "/openapi.json")
from web.tabword_routes import router as tabword_router
app.include_router(tabword_router)
STATIC = Path(__file__).resolve().parent / "static"

JOBS = {}
_LOCK = threading.Lock()

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# ---- 公网暴露时的保护措施（全部可关，默认本地免密）----
# 设置这两个环境变量即开启 HTTP Basic 口令；留空则任何人可访问。
AUTH_USER = os.environ.get("THESIS_AUTH_USER", "").strip()


def _secret_value(name):
    value = os.environ.get(name, "").strip()
    file_path = os.environ.get(name + "_FILE", "").strip()
    if value and file_path:
        raise RuntimeError("Set either %s or %s_FILE, not both" % (name, name))
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise RuntimeError("Cannot read configured secret file for %s" % name) from exc
    return value


AUTH_PASS = _secret_value("THESIS_AUTH_PASS")
AUTH_ON = bool(AUTH_USER and AUTH_PASS)
AFFILIATE_ADMIN_USER = os.environ.get("AFFILIATE_ADMIN_USER", "").strip()
AFFILIATE_ADMIN_PASS = _secret_value("AFFILIATE_ADMIN_PASS")
AFFILIATE_ADMIN_ON = bool(AFFILIATE_ADMIN_USER and AFFILIATE_ADMIN_PASS)
AUTH_REQUIRED = os.environ.get("THESIS_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")
if AUTH_REQUIRED and not AUTH_ON:
    raise RuntimeError("THESIS_REQUIRE_AUTH is enabled but the Basic Auth credentials are unavailable")
ALLOWED_HOSTS = [host.strip() for host in
                 os.environ.get("THESIS_ALLOWED_HOSTS", "*").split(",") if host.strip()]
if not ALLOWED_HOSTS:
    raise RuntimeError("THESIS_ALLOWED_HOSTS must contain at least one host")
if "*" not in ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
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
        if (not AUTH_ON or request.url.path in ("/api/health", "/api/alipay/notify")
                or AffiliateAdminAuthMiddleware._is_admin_path(request.url.path)):
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


class AffiliateAdminAuthMiddleware(BaseHTTPMiddleware):
    @staticmethod
    def _is_public_auth_path(path):
        return path in ("/api/admin/auth/status", "/api/admin/auth/register", "/api/admin/auth/verify")

    @staticmethod
    def _is_admin_path(path):
        return (path in ("/admin/affiliates", "/affiliates.js")
                or path.startswith("/api/admin/"))

    async def dispatch(self, request, call_next):
        if not self._is_admin_path(request.url.path):
            return await call_next(request)
        if self._is_public_auth_path(request.url.path):
            return await call_next(request)
        if request.url.path in ("/admin/affiliates", "/affiliates.js") and not AFFILIATE_ADMIN_ON:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if AFFILIATE_ADMIN_ON and header[:6].lower() == "basic ":
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, pwd = raw.partition(":")
                ok = (secrets.compare_digest(user, AFFILIATE_ADMIN_USER)
                      and secrets.compare_digest(pwd, AFFILIATE_ADMIN_PASS))
            except Exception:
                ok = False
        elif header[:7].lower() == "bearer " and not AFFILIATE_ADMIN_ON:
            ok = affiliates.authenticate_admin_token(header[7:].strip())
        if not ok:
            headers = ({"WWW-Authenticate": 'Basic realm="affiliate-admin", charset="UTF-8"'}
                       if AFFILIATE_ADMIN_ON else {})
            return Response(content="需要管理员登录", status_code=401,
                            media_type="text/plain; charset=utf-8", headers=headers)
        return await call_next(request)


app.add_middleware(BasicAuthMiddleware)
app.add_middleware(AffiliateAdminAuthMiddleware)


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
    oldest = sorted((j for j in JOBS.values() if j["state"] != "running"), key=lambda j: j.get("t", 0))[:len(JOBS) - KEEP_JOBS]
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


class AffiliateCreateReq(BaseModel):
    name: str
    email: str = ""
    code: str | None = None
    commission_rate: float = 10.0


class AffiliateUpdateReq(BaseModel):
    active: bool | None = None
    commission_rate: float | None = None


class CommissionUpdateReq(BaseModel):
    status: str
    payout_ref: str = ""


class PaymentCreateReq(BaseModel):
    coupon_code: str = ""


class CouponPreviewReq(BaseModel):
    code: str


class CouponCreateReq(BaseModel):
    code: str
    discount_kind: str
    discount_value: float
    school_key: str | None = None
    max_uses: int | None = None


class CouponUpdateReq(BaseModel):
    active: bool


class PortalWithdrawalReq(BaseModel):
    payout_info: str


class WithdrawalUpdateReq(BaseModel):
    status: str


def _new_job(kind="build"):
    with _LOCK:
        jid = "j" + secrets.token_hex(16)
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "files": {},
               "tmp": tempfile.mkdtemp(prefix="thesis-web-"), "t": time.time()}
        JOBS[jid] = job
    _reap_jobs()
    return job


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/app.js")
def frontend_script():
    return FileResponse(STATIC / "app.js", media_type="application/javascript")


def _require_affiliate_admin():
    # Authentication is enforced by AffiliateAdminAuthMiddleware.
    return None


@app.get('/api/admin/service-prices')
def service_prices():
    _require_affiliate_admin()
    from core import service_billing
    return service_billing.prices()


@app.get('/api/admin/service-orders')
def service_orders():
    _require_affiliate_admin()
    from core import service_billing
    return {'orders': service_billing.admin_orders()}


@app.get('/api/admin/service-orders.csv')
def service_orders_csv():
    _require_affiliate_admin()
    from core import service_billing
    fields = ('订单号', '服务', '金额(元)', '字符数', '支付状态', '处理状态',
              '推广员', '推广员邮箱', '推广码', '活动', '佣金(元)', '佣金状态', '创建时间')
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(fields)
    labels = {'detect_text': '文本检测', 'detect_file': '文件检测'}
    states = {'ready': '待提交', 'processing': '处理中', 'submitted': '已提交',
              'complete': '已完成', 'failed': '异常，待核查'}
    for order in service_billing.admin_orders():
        writer.writerow([
            order.get('out_trade_no') or '', labels.get(order['operation'], '论文改写'),
            f"{int(order['amount_cents'] or 0) / 100:.2f}", order.get('characters') or 0,
            '已支付' if order.get('payment_status') == 'TRADE_SUCCESS' else '未支付',
            states.get(order.get('state'), order.get('state') or ''),
            order.get('affiliate_name') or '', order.get('affiliate_email') or '',
            order.get('affiliate_code') or '', order.get('affiliate_campaign') or '',
            f"{int(order['commission_cents'] or 0) / 100:.2f}",
            order.get('commission_status') or '',
            time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(order['created_at'])),
        ])
    return Response('\ufeff' + output.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="service-orders.csv"'})


@app.put('/api/admin/service-prices')
def update_service_prices(body: dict):
    _require_affiliate_admin()
    from core import service_billing, tabword
    try:
        return service_billing.save_prices(body)
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@app.get("/admin/affiliates", response_class=HTMLResponse)
def affiliate_admin_page():
    _require_affiliate_admin()
    return (STATIC / "affiliates.html").read_text(encoding="utf-8")


@app.get("/affiliates.js")
def affiliate_admin_script():
    return FileResponse(STATIC / "affiliates.js", media_type="application/javascript")


@app.get("/api/admin/auth/status")
def api_admin_auth_status():
    return {"mode": "basic" if AFFILIATE_ADMIN_ON else "token",
            "registered": affiliates.has_admin_token()}


@app.post("/api/admin/auth/register")
def api_admin_auth_register(request: Request, body: dict):
    if AFFILIATE_ADMIN_ON:
        raise HTTPException(409, "后台已使用服务器配置的管理员口令")
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")
    source = forwarded[-1].strip() if forwarded and forwarded[-1].strip() else peer
    try:
        local_request = source == "testclient" or ipaddress.ip_address(source).is_loopback
    except ValueError:
        local_request = False
    if not local_request:
        raise HTTPException(403, "管理员注册仅允许从服务器本机完成")
    try:
        token = affiliates.register_admin_token(str(body.get("name", "管理员")))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"access_token": token}


@app.post("/api/admin/auth/verify")
def api_admin_auth_verify(request: Request):
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
    if AFFILIATE_ADMIN_ON:
        return {"valid": False, "mode": "basic"}
    return {"valid": affiliates.authenticate_admin_token(token), "mode": "token"}


@app.get("/affiliate", response_class=HTMLResponse)
def affiliate_portal_page():
    return (STATIC / "affiliate.html").read_text(encoding="utf-8")


@app.get("/affiliate.js")
def affiliate_portal_script():
    return FileResponse(STATIC / "affiliate.js", media_type="application/javascript")


@app.get("/r/{code}")
def affiliate_referral(code: str, request: Request, campaign: str = Query("", max_length=100)):
    referral = affiliates.record_click(code, campaign)
    response = RedirectResponse("/", status_code=302)
    if referral:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie("affiliate_ref", referral["code"], max_age=30 * 86400,
                            httponly=True, secure=secure, samesite="lax", path="/")
        response.set_cookie("affiliate_campaign", referral["campaign"], max_age=30 * 86400,
                            httponly=True, secure=secure, samesite="lax", path="/")
    return response


@app.get("/api/admin/affiliates")
def api_admin_affiliates():
    _require_affiliate_admin()
    return {"affiliates": affiliates.list_affiliates()}


@app.post("/api/admin/affiliates")
def api_admin_create_affiliate(body: AffiliateCreateReq):
    _require_affiliate_admin()
    try:
        return affiliates.create_affiliate(body.name, body.email, body.code, body.commission_rate)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "推广码已被使用") from None


@app.patch("/api/admin/affiliates/{affiliate_id}")
def api_admin_update_affiliate(affiliate_id: int, body: AffiliateUpdateReq):
    _require_affiliate_admin()
    try:
        affiliate = affiliates.update_affiliate(affiliate_id, active=body.active,
                                                commission_rate=body.commission_rate)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not affiliate:
        raise HTTPException(404, "推广员不存在")
    return affiliate


@app.post("/api/admin/affiliates/{affiliate_id}/portal-token")
def api_admin_rotate_portal_token(affiliate_id: int):
    _require_affiliate_admin()
    token = affiliates.rotate_portal_token(affiliate_id)
    if not token:
        raise HTTPException(404, "推广员不存在或已停用")
    return {"portal_token": token}


@app.get("/api/admin/commissions")
def api_admin_commissions(status: str | None = Query(None)):
    _require_affiliate_admin()
    try:
        return {"commissions": affiliates.list_commissions(status)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.patch("/api/admin/commissions/{commission_id}")
def api_admin_update_commission(commission_id: int, body: CommissionUpdateReq):
    _require_affiliate_admin()
    try:
        commission = affiliates.update_commission(commission_id, body.status, body.payout_ref)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not commission:
        raise HTTPException(404, "佣金记录不存在")
    return commission


@app.get("/api/admin/coupons")
def api_admin_coupons():
    _require_affiliate_admin()
    return {"coupons": affiliates.list_coupons()}


@app.post("/api/admin/coupons")
def api_admin_create_coupon(body: CouponCreateReq):
    _require_affiliate_admin()
    try:
        return affiliates.create_coupon(body.code, body.discount_kind, body.discount_value,
                                        body.school_key, body.max_uses)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "优惠码已存在") from None


@app.patch("/api/admin/coupons/{coupon_id}")
def api_admin_update_coupon(coupon_id: int, body: CouponUpdateReq):
    _require_affiliate_admin()
    coupon = affiliates.update_coupon(coupon_id, body.active)
    if not coupon:
        raise HTTPException(404, "优惠券不存在")
    return coupon


def _affiliate_from_request(request: Request):
    authorization = request.headers.get("authorization", "")
    if authorization[:7].lower() != "bearer ":
        raise HTTPException(401, "请使用推广员访问令牌登录")
    affiliate = affiliates.authenticate_portal(authorization[7:].strip())
    if not affiliate:
        raise HTTPException(401, "推广员令牌无效或账号已停用")
    return affiliate


@app.get("/api/affiliate/me")
def api_affiliate_me(request: Request):
    affiliate = _affiliate_from_request(request)
    return {"affiliate": affiliate, **affiliates.affiliate_summary(affiliate["id"])}


@app.post("/api/affiliate/withdrawals")
def api_affiliate_request_withdrawal(body: PortalWithdrawalReq, request: Request):
    affiliate = _affiliate_from_request(request)
    try:
        return affiliates.request_withdrawal(affiliate["id"], body.payout_info)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/admin/withdrawals")
def api_admin_withdrawals(status: str | None = Query(None)):
    _require_affiliate_admin()
    try:
        return {"withdrawals": affiliates.list_withdrawals(status)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.patch("/api/admin/withdrawals/{withdrawal_id}")
def api_admin_update_withdrawal(withdrawal_id: int, body: WithdrawalUpdateReq):
    _require_affiliate_admin()
    try:
        withdrawal = affiliates.update_withdrawal(withdrawal_id, body.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not withdrawal:
        raise HTTPException(404, "提现申请不存在")
    return withdrawal


@app.post("/api/admin/withdrawals/{withdrawal_id}/paid")
async def api_admin_finish_withdrawal(withdrawal_id: int, payout_ref: str = Form(...),
                                      proof: UploadFile = File(...)):
    _require_affiliate_admin()
    suffix = Path(proof.filename or "").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".pdf"):
        raise HTTPException(400, "凭证仅支持 PNG、JPG 或 PDF")
    content = await proof.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "凭证文件不能超过 5 MB")
    valid = ((suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
             or (suffix in (".jpg", ".jpeg") and content.startswith(b"\xff\xd8\xff"))
             or (suffix == ".pdf" and content.startswith(b"%PDF-")))
    if not valid:
        raise HTTPException(400, "凭证内容与文件类型不匹配")
    proof_dir = Path(os.environ.get("AFFILIATE_PROOF_DIR", str(ROOT / ".secrets" / "affiliate-proofs")))
    proof_dir.mkdir(parents=True, exist_ok=True)
    proof_path = proof_dir / (secrets.token_hex(16) + suffix)
    proof_path.write_bytes(content)
    try:
        withdrawal = affiliates.finish_withdrawal(withdrawal_id, payout_ref, str(proof_path))
    except ValueError as exc:
        proof_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from None
    if not withdrawal:
        proof_path.unlink(missing_ok=True)
        raise HTTPException(404, "提现申请不存在")
    return withdrawal


@app.get("/api/admin/withdrawals/{withdrawal_id}/proof")
def api_admin_withdrawal_proof(withdrawal_id: int):
    _require_affiliate_admin()
    withdrawals = affiliates.list_withdrawals()
    withdrawal = next((row for row in withdrawals if row["id"] == withdrawal_id), None)
    if not withdrawal or not withdrawal["proof_path"]:
        raise HTTPException(404, "凭证不存在")
    path = Path(withdrawal["proof_path"]).resolve()
    proof_dir = Path(os.environ.get("AFFILIATE_PROOF_DIR", str(ROOT / ".secrets" / "affiliate-proofs"))).resolve()
    if proof_dir not in path.parents or not path.is_file():
        raise HTTPException(404, "凭证不存在")
    return FileResponse(path, filename="withdrawal-proof" + path.suffix)


@app.get("/api/schools")
def schools():
    raise HTTPException(410, "本地排版已停用，请使用 /api/format/rules 查询院校规范")


@app.post("/api/convert")
async def api_convert(file: UploadFile = File(...), school: str = Form("hkjjxy")):
    raise HTTPException(410, "本地转换已停用，请使用 /api/format 提交 API 排版")


@app.post("/api/build")
def api_build(req: BuildReq):
    raise HTTPException(410, "本地构建已停用，请使用 /api/format 提交 API 排版")


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
# 云端任务复用 JOBS（kind="cloud"），由独立额度限制轮询并发。
def _dp_call(fn, *args, **kwargs):
    """把接口异常翻成 HTTP 错误：鉴权类原样透出，其余归为 502。"""
    try:
        return fn(*args, **kwargs)
    except dayipaper.ApiError as exc:
        status = exc.http_status if exc.http_status in (400, 401, 403, 413, 429) else 502
        raise HTTPException(status, "排版服务请求失败，请检查配置或稍后重试")


def _format_job(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("kind") not in ("school", "cloud"):
        raise HTTPException(404, "排版任务不存在")
    return job


def _cloud_job(job_id):
    job = _format_job(job_id)
    if job.get("kind") != "cloud":
        raise HTTPException(404, "云端排版任务不存在")
    return job


@app.get("/api/dayipaper")
def api_dp_info():
    return {"enabled": dayipaper.has_token(), "base": dayipaper.base_url(),
            "maxJobs": MAX_CLOUD, "poll": max(CLOUD_POLL, dayipaper.MIN_INTERVAL),
            "timeout": CLOUD_TIMEOUT, "running": _cloud_running_count()}


@app.get("/api/alipay")
def api_alipay_info():
    return alipay.public_status()


@app.post("/api/format/jobs/{job_id}/coupon")
def api_preview_coupon(job_id: str, body: CouponPreviewReq):
    job = _format_job(job_id)
    if job.get("state") != "done":
        raise HTTPException(409, "排版任务尚未完成")
    price = alipay.public_status()["amount_cents"]
    try:
        quote = affiliates.preview_coupon(body.code, job.get("school_key", ""), price)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"list_price_cents": price, **quote}


@app.post("/api/format/jobs/{job_id}/payment")
def api_create_payment(job_id: str, request: Request, body: PaymentCreateReq | None = None):
    job = _format_job(job_id)
    if job.get("state") != "done" or (job.get("kind") == "cloud" and not job.get("taskId")):
        raise HTTPException(409, "排版任务尚未完成")
    try:
        order = alipay.create_order(job_id, job.get("name") or "论文",
                                    request.cookies.get("affiliate_ref"),
                                    request.cookies.get("affiliate_campaign", ""),
                                    body.coupon_code if body else "",
                                    job.get("school_key", ""))
    except alipay.AlipayError as exc:
        raise HTTPException(503, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"order_id": order["out_trade_no"], "status": order["status"],
            "amount_cents": order["amount_cents"],
            "list_price_cents": order.get("list_price_cents") or order["amount_cents"],
            "discount_cents": order.get("discount_cents", 0),
            "coupon_code": order.get("coupon_code"),
            "checkout_url": f"/api/format/jobs/{job_id}/payment/{order['out_trade_no']}/checkout"}


@app.get("/api/format/jobs/{job_id}/payment/{order_id}")
def api_payment_status(job_id: str, order_id: str):
    _format_job(job_id)
    order = alipay.get_order(order_id, job_id)
    if not order:
        raise HTTPException(404, "支付订单不存在")
    return {"order_id": order["out_trade_no"], "status": order["status"],
            "paid": order["status"] == "TRADE_SUCCESS",
            "expired": (order["status"] != "TRADE_SUCCESS" and
                        (time.time() >= order["created_at"] + alipay.ORDER_LIFETIME
                         or order["payment_product"] != "page_pay"))}


@app.get("/api/format/jobs/{job_id}/payment/{order_id}/checkout")
def api_payment_checkout(job_id: str, order_id: str):
    _format_job(job_id)
    try:
        url = alipay.checkout_url(order_id, job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except alipay.AlipayError as exc:
        raise HTTPException(503, str(exc)) from None
    return RedirectResponse(url, status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/api/format/jobs/{job_id}/payment/{order_id}/qr.png")
def api_payment_qr(job_id: str, order_id: str):
    _format_job(job_id)
    try:
        image = alipay.qr_png(order_id, job_id)
    except ImportError:
        raise HTTPException(503, "二维码组件未安装") from None
    if image is None:
        raise HTTPException(404, "支付订单不存在")
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/alipay/notify")
async def api_alipay_notify(request: Request):
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    return Response("success" if alipay.apply_notification(params) else "failure",
                    media_type="text/plain; charset=utf-8")


@app.get("/api/dayipaper/rules")
def api_dp_rules(q: str = Query(..., min_length=1, description="学校名或关键词"),
                 size: int = Query(5, ge=1, le=20)):
    if not dayipaper.has_token():
        raise HTTPException(503, "云端排版未启用：请设置环境变量 DAYIPAPER_PAT 后重启服务")
    return _dp_call(dayipaper.search_rules, q, page_size=size)


@app.get('/api/format/rules')
def api_format_rules(q: str = Query(..., min_length=1), size: int = Query(5, ge=1, le=20)):
    result = api_dp_rules(q, size)
    # All schools, including Haikou Economics University, use the provider API.
    return {**result, 'items': [{**r, 'id': 'dayipaper:' + str(r['id']),
                                'provider': 'dayipaper'} for r in result.get('items', [])]}


@app.post("/api/format")
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

    school_key = meta['ruleId']
    if meta['ruleId'].startswith('dayipaper:'):
        meta['ruleId'] = meta['ruleId'].split(':', 1)[1]
        school_key = 'dayipaper:' + meta['ruleId']
    elif ':' in meta['ruleId']:
        raise HTTPException(400, '未注册的排版服务')
    else:
        school_key = 'dayipaper:' + meta['ruleId']

    job = _new_job(kind="cloud")
    job.update({"name": meta["title"], "school_key": school_key,
                "stage": "上传源文档", "status": None,
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


@app.get("/api/format/jobs/{job_id}/preview")
@app.get("/api/dayipaper/preview/{job_id}")
def api_dp_preview(job_id: str):
    """现取一次状态再跳转：接口给的 pdfShareUrl 只有 3 分钟有效期，存下来会点开失败。"""
    local = JOBS.get(job_id)
    if local and local.get('kind') == 'school':
        if 'pdf' not in local['files']:
            raise HTTPException(409, '预览尚未生成')
        return FileResponse(local['files']['pdf'], media_type='application/pdf')
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


@app.get("/api/format/jobs/{job_id}/download")
@app.get("/api/dayipaper/download/{job_id}")
def api_dp_download(job_id: str, fmt: int = Query(1, ge=1, le=2),
                    confirm: bool = Query(False, description="导出计费，必须显式确认"),
                    payment_order: str | None = Query(None)):
    local = JOBS.get(job_id)
    if local and local.get('kind') == 'school':
        if fmt == 2:
            if 'pdf' not in local['files']:
                raise HTTPException(409, 'PDF 预览尚未生成')
            return FileResponse(local['files']['pdf'], media_type='application/pdf',
                                filename=sanitize(local['name']) + '.pdf')
        if fmt != 1 or 'docx' not in local['files']:
            raise HTTPException(409, 'Word 尚未生成或格式不支持')
        if not alipay.public_status()["enabled"]:
            raise HTTPException(503, "支付宝电脑网站支付尚未配置完成，暂不能下载 Word")
        order = alipay.get_order(payment_order, job_id)
        if not order or order["status"] != "TRADE_SUCCESS":
            raise HTTPException(402, "请先完成支付宝电脑网站支付")
        return FileResponse(local['files']['docx'], filename=sanitize(local['name']) + '.docx')
    job = _cloud_job(job_id)
    with _LOCK:
        export_lock = job.setdefault("exportLock", threading.Lock())
    with export_lock:
        if not alipay.public_status()["enabled"]:
            raise HTTPException(503, "支付宝电脑网站支付尚未配置完成，暂不能下载 Word")
        order = alipay.get_order(payment_order, job_id)
        if not order or order["status"] != "TRADE_SUCCESS":
            raise HTTPException(402, "请先完成支付宝电脑网站支付")
        confirm = True
        return _export_job(job, fmt, confirm)


def _export_job(job, fmt, confirm):
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
        raise HTTPException(502, "导出失败，请稍后重试")
    job["files"][kind] = path
    job["exported"] = True
    job["log"].append("已导出成品：%s" % os.path.basename(path))
    return FileResponse(path, filename=os.path.basename(path))


@app.get("/api/format/jobs/{job_id}")
def api_format_job(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get('kind') not in ('school', 'cloud'):
        raise HTTPException(404, '排版任务不存在')
    return {"id": job["id"], "state": job["state"],
            "stage": job.get("stage"), "statusLabel": job.get("statusLabel"),
            "preview": bool(job.get("preview") or job.get('files', {}).get('pdf')),
            "billedExport": job.get('kind') == 'cloud',
            "paymentRequired": True,
            "freePdfDownload": job.get('kind') == 'school' and bool(job.get('files', {}).get('pdf')),
            "suggestions": job.get("suggestions") or [],
            "exported": bool(job.get("exported"))}


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

