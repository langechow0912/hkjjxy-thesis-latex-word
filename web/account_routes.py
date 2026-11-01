"""用户注册 / 登录 / 我的记录。

排版任务表 JOBS 在 web/app.py 里；为避免循环导入，app.py 启动时调用 bind() 传进来。
"""
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core import accounts, alipay, service_billing as billing, tabword

router = APIRouter(prefix='/api/account')
_JOBS = {}


def bind(jobs):
    global _JOBS
    _JOBS = jobs


def current_user(request: Request):
    """上传、下单前要求登录"""
    user = accounts.session_user(request.cookies.get(accounts.COOKIE))
    if not user:
        raise HTTPException(401, '请先登录')
    return user


class Credentials(BaseModel):
    username: str
    password: str


def _signed_in(request, response, token, user):
    secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
    response.set_cookie(accounts.COOKIE, token, max_age=accounts.SESSION_DAYS * 86400,
                        httponly=True, samesite='lax', secure=secure, path='/')
    return {'user': user}


@router.post('/register')
def register(body: Credentials, request: Request, response: Response):
    try:
        token, user = accounts.register(body.username, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return _signed_in(request, response, token, user)


@router.post('/login')
def login(body: Credentials, request: Request, response: Response):
    try:
        token, user = accounts.login(body.username, body.password)
    except accounts.AccountError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return _signed_in(request, response, token, user)


@router.post('/logout')
def logout(request: Request, response: Response):
    accounts.logout(request.cookies.get(accounts.COOKIE))
    response.delete_cookie(accounts.COOKIE, path='/')
    return {'ok': True}


@router.get('/me')
def me(request: Request):
    return {'user': accounts.session_user(request.cookies.get(accounts.COOKIE))}


def _entry(row):
    order = accounts.paid_order(row['ref'])
    item = {**row, 'paid': bool(order), 'order_id': order}
    if row['kind'] == 'format':
        job = _JOBS.get(row['ref'])
        item['state'] = job['state'] if job else 'expired'
        return item
    if not order:
        return None             # 没付款的降重 / 检测报价只是一次询价，不算记录
    try:
        item['state'] = billing.get_quote(row['ref'])['state']
    except tabword.TabwordError:
        return None
    return item


@router.get('/history')
def history(user=Depends(current_user)):
    return {'items': [e for e in map(_entry, accounts.uploads(user['id'])) if e]}


@router.delete('/history/{ref}')
def delete_history(ref: str, user=Depends(current_user)):
    row = accounts.get_upload(user['id'], ref)
    if not row:
        raise HTTPException(404, '记录不存在')
    if accounts.paid_order(ref):
        raise HTTPException(409, '已付款的记录不能删除')
    if accounts.open_order(ref, alipay.ORDER_LIFETIME):
        raise HTTPException(409, '这条记录有刚创建的付款订单，%d 分钟内不能删除' % (alipay.ORDER_LIFETIME // 60))
    if row['kind'] == 'format':
        job = _JOBS.get(ref)
        if job and job['state'] == 'running':
            raise HTTPException(409, '排版还在进行中，完成后再删除')
        if job:
            _JOBS.pop(ref, None)
            shutil.rmtree(job.get('tmp', ''), ignore_errors=True)
    accounts.delete_upload(user['id'], ref)
    return {'ok': True}
