# -*- coding: utf-8 -*-
"""积分：余额、充值、用积分付款。路由只做归属 / 状态校验，记账都在 core/points.py。"""
import time
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core import accounts, alipay, points, service_billing as billing, tabword
from web.account_routes import current_user

router = APIRouter(prefix='/api/points')
QUOTE_LIFETIME = 15 * 60
_format_job = None


def bind(get_format_job):
    """由 web/app.py 注入「取排版任务」函数，避免反向 import app。"""
    global _format_job
    _format_job = get_format_job


def _fail(exc):
    raise HTTPException(exc.status, str(exc)) from None


@router.get('')
def summary(user=Depends(current_user)):
    p = billing.prices()
    prices = {'detect_text': p['detect_text_cents'], 'detect_file': p['detect_file_cents'],
              'format_export': p['format_export_cents'], 'rewrite_per_1000': p['rewrite_per_1000_cents']}
    return {**points.summary(user['id']),
            'alipay_enabled': alipay.public_status()['enabled'],
            'prices': {name: points.points_for_cents(cents) for name, cents in prices.items()},
            'prices_cents': prices}


class TopupReq(BaseModel):
    yuan: int


@router.post('/topup')
def topup(body: TopupReq, request: Request, user=Depends(current_user)):
    try:
        return points.create_topup(user['id'], body.yuan, request.cookies.get('affiliate_ref'),
                                   request.cookies.get('affiliate_campaign', ''))
    except points.PointsError as exc:
        _fail(exc)


class RedeemReq(BaseModel):
    code: str


@router.post('/redeem')
def redeem(body: RedeemReq, user=Depends(current_user)):
    try:
        return points.redeem(user['id'], body.code[:64])
    except points.PointsError as exc:
        _fail(exc)


def _own_topup(user, order_id):
    with closing(points._connect()) as conn:
        row = conn.execute('SELECT user_id FROM point_topups WHERE order_no=?', (order_id,)).fetchone()
    if not row or row['user_id'] != user['id']:
        raise HTTPException(404, '充值订单不存在')
    return alipay.get_order(order_id)


@router.get('/topup/{order_id}')
def topup_status(order_id: str, user=Depends(current_user)):
    order = _own_topup(user, order_id)
    order = alipay.sync_order(order_id) or order
    points.reconcile(user['id'])
    return {'order_id': order_id, 'paid': order['status'] == 'TRADE_SUCCESS', 'status': order['status'],
            'balance': points.balance(user['id']),
            'expired': (order['status'] != 'TRADE_SUCCESS'
                        and time.time() >= order['created_at'] + alipay.ORDER_LIFETIME)}


@router.get('/topup/{order_id}/checkout')
def topup_checkout(order_id: str, user=Depends(current_user)):
    order = _own_topup(user, order_id)
    try:
        url = alipay.checkout_url(order_id, order['job_id'])
    except (ValueError, alipay.AlipayError) as exc:
        raise HTTPException(409, str(exc)) from None
    return RedirectResponse(url, status_code=303,
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


class PayReq(BaseModel):
    quote_id: str | None = None
    job_id: str | None = None


def _quote_amount(user, quote_id):
    if not quote_id.startswith('TW-') or not accounts.get_upload(user['id'], quote_id):
        raise HTTPException(404, '服务订单不存在')
    if not tabword.enabled('TABWORD_ENABLE_CALLS') or not tabword.api_key():
        raise HTTPException(503, '处理服务暂未启用，暂不接受付款')
    try:
        q = billing.get_quote(quote_id)
    except tabword.TabwordError as exc:
        _fail(exc)
    if q['state'] == 'expired':
        raise HTTPException(410, billing.EXPIRED)
    if not alipay.job_paid(quote_id):
        if q['state'] != 'ready':
            raise HTTPException(409, '该订单已提交处理，请勿再次购买')
        if time.time() - q['created_at'] > QUOTE_LIFETIME:
            raise HTTPException(409, '报价已过期，请重新计价')
    return q['amount_cents']


def _export_amount(user, job_id):
    job = _format_job(job_id) if _format_job else None
    if not job or not accounts.get_upload(user['id'], job_id):
        raise HTTPException(404, '排版任务不存在')
    if job.get('state') != 'done' or (job.get('kind') == 'cloud' and not job.get('taskId')):
        raise HTTPException(409, '排版任务尚未完成')
    return billing.prices()['format_export_cents']


@router.post('/pay')
def pay(body: PayReq, user=Depends(current_user)):
    if bool(body.quote_id) == bool(body.job_id):
        raise HTTPException(422, '请指定要支付的服务订单或排版任务')
    target = body.quote_id or body.job_id
    amount = _quote_amount(user, target) if body.quote_id else _export_amount(user, target)
    try:
        order = points.pay(user['id'], target, amount)
    except points.PointsError as exc:
        _fail(exc)
    return {'order_id': order['out_trade_no'], 'status': order['status'], 'paid': True,
            'amount_cents': order['amount_cents'], 'points': points.points_for_cents(order['amount_cents']),
            'balance': points.balance(user['id']), 'job_id': target}
