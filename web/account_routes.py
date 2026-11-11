"""用户注册 / 登录 / 我的记录。

排版任务表 JOBS 在 web/app.py 里；为避免循环导入，app.py 启动时调用 bind() 传进来。
"""
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


class EmailReq(BaseModel):
    email: str


class EmailCodeReq(BaseModel):
    email: str
    code: str


def _fail(exc):
    raise HTTPException(exc.status, str(exc)) from None


@router.post('/email/send')
def email_send(body: EmailReq):
    try:
        return accounts.send_email_code(body.email, 'login')
    except accounts.AccountError as exc:
        _fail(exc)


@router.post('/email/login')
def email_login(body: EmailCodeReq, request: Request, response: Response):
    try:
        token, user, created = accounts.login_with_email(body.email, body.code)
    except accounts.AccountError as exc:
        _fail(exc)
    return {**_signed_in(request, response, token, user), 'created': created}


@router.post('/email/bind/send')
def email_bind_send(body: EmailReq, user=Depends(current_user)):
    try:
        return accounts.send_email_code(body.email, 'bind', user['id'])
    except accounts.AccountError as exc:
        _fail(exc)


@router.post('/email/bind/verify')
def email_bind_verify(body: EmailCodeReq, user=Depends(current_user)):
    try:
        return accounts.bind_email(user['id'], body.email, body.code)
    except accounts.AccountError as exc:
        _fail(exc)


@router.post('/logout')
def logout(request: Request, response: Response):
    accounts.logout(request.cookies.get(accounts.COOKIE))
    response.delete_cookie(accounts.COOKIE, path='/')
    return {'ok': True}


@router.get('/me')
def me(request: Request):
    return {'user': accounts.session_user(request.cookies.get(accounts.COOKIE))}


def _entry(row):
    """只列已付款的；付款满 RESULT_TTL 的标为 expired（文件和结果已删除）"""
    order = accounts.paid_order(row['ref'])
    if not order:
        return None
    paid_at = alipay.paid_at(row['ref'])
    item = {**row, 'order_id': order, 'paid_at': paid_at, 'expires_at': paid_at + alipay.RESULT_TTL}
    if alipay.result_expired(row['ref']):
        item['state'] = 'expired'
    elif row['kind'] == 'format':
        job = _JOBS.get(row['ref'])
        item['state'] = job['state'] if job else 'expired'
    else:
        try:
            item['state'] = billing.get_quote(row['ref'])['state']
        except tabword.TabwordError:
            return None
    return item


@router.get('/history')
def history(user=Depends(current_user)):
    return {'items': [e for e in map(_entry, accounts.uploads(user['id'])) if e],
            'keep_hours': alipay.RESULT_TTL // 3600}
