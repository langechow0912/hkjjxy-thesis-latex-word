"""Local preparation and gated execution for the allowlisted Tabword API."""
import json
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import StreamingResponse, RedirectResponse, Response
from core import tabword, alipay, service_billing as billing

router = APIRouter(prefix='/api/tabword')


@router.get('')
def status():
    return {**tabword.catalog(), 'prices': billing.prices(), 'payment_required': True}


@router.post('/billing/quote')
def price_quote(operation: str = Form(...), params: str = Form('{}'), file: UploadFile | None = File(None)):
    try:
        try:
            values = json.loads(params)
        except ValueError:
            raise tabword.TabwordError('参数不是有效 JSON', 422)
        content = (file.filename or '', file.file.read(20*1024*1024+1)) if file else None
        return billing.quote(operation, values, content)
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.post('/billing/{quote_id}/payment')
def create_payment(quote_id: str, request: Request):
    try:
        if not tabword.enabled('TABWORD_ENABLE_CALLS') or not tabword.api_key():
            raise tabword.TabwordError('处理服务暂未启用，暂不接受付款', 503)
        order = billing.create_payment(quote_id, request.cookies.get('affiliate_ref'), request.cookies.get('affiliate_campaign', ''))
        return {'order_id': order['out_trade_no'], 'amount_cents': order['amount_cents'],
                'status': order['status'], 'checkout_url': f"/api/tabword/billing/{quote_id}/{order['out_trade_no']}/checkout"}
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except (alipay.AlipayError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from None


@router.get('/billing/{quote_id}/{order_id}')
def payment_status(quote_id: str, order_id: str):
    order = alipay.get_order(order_id, quote_id)
    if not order or not quote_id.startswith('TW-'):
        raise HTTPException(404, '订单不存在')
    q = billing.get_quote(quote_id)
    return {'status': order['status'], 'service_state': q['state'], 'task_id': q['task_id'],
            'task_operation': q['task_operation'], 'error': q['error']}


@router.get('/billing/{quote_id}/{order_id}/result')
def saved_result(quote_id: str, order_id: str):
    order = alipay.get_order(order_id, quote_id)
    if not order or order['status'] != 'TRADE_SUCCESS':
        raise HTTPException(402, '请先完成此订单付款')
    try:
        q = billing.get_quote(quote_id)
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    headers = {'Cache-Control': 'no-store'}
    if q['result_sse']:
        return Response(q['result_sse'], media_type='text/event-stream', headers=headers)
    if q['result_json']:
        return Response(q['result_json'], media_type='application/json', headers=headers)
    raise HTTPException(409, '结果暂未就绪；已提交的订单请勿重复提交')


@router.get('/billing/{quote_id}/{order_id}/checkout')
def checkout(quote_id: str, order_id: str):
    try:
        billing.get_quote(quote_id)
        return RedirectResponse(alipay.checkout_url(order_id, quote_id), status_code=303,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except (alipay.AlipayError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.get('/catalog/{operation}')
def read_catalog(operation: str, mode: str | None = None, language: str | None = None):
    try:
        values = {'presets': {'mode': mode}, 'detection_options': {'language': language}}.get(operation)
        if values is None:
            raise tabword.TabwordError('此接口不属于免费只读目录',403)
        return tabword.read_catalog(operation, {key:value for key,value in values.items() if value is not None})
    except tabword.TabwordError as exc:
        raise HTTPException(status_code=exc.status,detail=str(exc)) from None


@router.post('/{operation}')
def dispatch(operation: str, params: str = Form('{}'), submit: bool = Form(False),
             file: UploadFile | None = File(None), payment_order: str = Form('')):
    try:
        try:
            values = json.loads(params)
        except ValueError:
            raise tabword.TabwordError('参数不是有效 JSON',422)
        content = None
        if file:
            content = (file.filename or '', file.file.read(20*1024*1024+1))
        if not submit:
            return tabword.dry_run(operation,values,content)
        if operation not in tabword.OPERATIONS:
            raise tabword.TabwordError('未知接口',404)
        if operation not in tabword.FREE_CATALOG_OPERATIONS:
            if operation not in billing.SUBMISSIONS | {'document_task', 'paraphrase_task'}:
                raise tabword.TabwordError('此接口未向网站用户开放', 403)
            tabword.prepare(operation, values, content)
            if not tabword.enabled('TABWORD_ENABLE_CALLS') or not tabword.api_key():
                raise tabword.TabwordError('处理服务暂未启用', 503)
            identity = billing.authorize(operation, values, content, payment_order)
        else:
            identity = None
        if tabword.OPERATIONS[operation].stream:
            try:
                stream = tabword.stream_call(operation,values,content)
            except Exception:
                billing.record(identity, operation, error=True)
                raise
            billing.record(identity, operation)
            return StreamingResponse(billing.capture_stream(identity, operation, stream),media_type='text/event-stream',
                                     headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        try:
            result = tabword.call(operation,values,content)
        except Exception:
            if identity:
                billing.record(identity, operation, error=True)
            raise
        if identity:
            billing.record(identity, operation, result)
        return result
    except tabword.TabwordError as exc:
        raise HTTPException(status_code=exc.status,detail=str(exc)) from None
