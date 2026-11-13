"""Local preparation and gated execution for the allowlisted Tabword API."""
import json
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import StreamingResponse, RedirectResponse, Response
from core import accounts, tabword, alipay, service_billing as billing
from web.account_routes import current_user

router = APIRouter(prefix='/api/tabword')


@router.get('')
def status():
    return {**tabword.catalog(), 'prices': billing.prices(), 'deadlines': billing.deadlines(),
            'unavailable_platforms': billing.unavailable_platforms(), 'payment_required': True}


@router.post('/billing/quote')
def price_quote(operation: str = Form(...), params: str = Form('{}'), file: UploadFile | None = File(None),
                user=Depends(current_user)):
    try:
        try:
            values = json.loads(params)
        except ValueError:
            raise tabword.TabwordError('参数不是有效 JSON', 422)
        content = (file.filename or '', file.file.read(20*1024*1024+1)) if file else None
        quote = billing.quote(operation, values, content, user_id=user['id'])
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    text = values.get('text') if isinstance(values, dict) else ''
    name = content[0] if content else ' '.join(str(text or '').split())[:30] or '文本'
    accounts.record_upload(user['id'], 'tabword', quote['quote_id'], name, operation)
    return quote


@router.post('/billing/{quote_id}/payment')
def create_payment(quote_id: str, request: Request, user=Depends(current_user)):
    if not accounts.get_upload(user['id'], quote_id):
        raise HTTPException(404, '服务订单不存在')
    try:
        if not tabword.enabled('TABWORD_ENABLE_CALLS') or not tabword.api_key():
            raise tabword.TabwordError('处理服务暂未启用，暂不接受付款', 503)
        order = billing.create_payment(quote_id, request.cookies.get('affiliate_ref'), request.cookies.get('affiliate_campaign', ''),
                                       resume_return=True)
        return {'order_id': order['out_trade_no'], 'amount_cents': order['amount_cents'],
                'status': order['status'], 'checkout_url': f"/api/tabword/billing/{quote_id}/{order['out_trade_no']}/checkout"}
    except tabword.TabwordError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except (alipay.AlipayError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from None


@router.get('/billing/{quote_id}/{order_id}')
def payment_status(quote_id: str, order_id: str):
    order = alipay.sync_order(order_id, quote_id)
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
    if q['state'] == 'expired':
        raise HTTPException(410, billing.EXPIRED)
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


def _context(values, content):
    """失败时一起记下选的平台 / 预设和文件大小，事后才能看出是哪一类请求出的问题（不含正文）。"""
    parts = []
    if isinstance(values, dict):
        if values.get('task_platform'):
            parts.append('平台 %s' % str(values['task_platform'])[:30])
        if values.get('preset'):
            parts.append('预设 %s' % str(values['preset'])[:40])
    if content:
        parts.append('文件 %d KB' % (len(content[1]) // 1024))
    return '（%s）' % '，'.join(parts) if parts else ''


@router.post('/{operation}')
def dispatch(operation: str, params: str = Form('{}'), submit: bool = Form(False),
             file: UploadFile | None = File(None), payment_order: str = Form('')):
    slot = None
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
            if operation in billing.SUBMISSIONS:
                # 先占名额再认领订单：名额满时订单还是 ready，用户稍后可以直接重试。
                slot = billing.GATE.acquire()
                if slot is None:
                    raise tabword.TabwordError(billing.BUSY_MESSAGE, 429)
            try:
                identity = billing.authorize(operation, values, content, payment_order)
            except BaseException:
                if slot:
                    billing.GATE.release(slot)
                raise
        else:
            identity = None
        ctx = _context(values, content)
        if tabword.OPERATIONS[operation].stream:
            try:
                stream = tabword.stream_call(operation,values,content)
            except tabword.TabwordError as exc:
                note = billing.record(identity, operation, error=True, reason=str(exc) + ctx) if identity else None
                raise tabword.TabwordError('%s %s' % (exc, note) if note else str(exc), exc.status) from None
            except Exception as exc:
                note = billing.record(identity, operation, error=True, reason='%s: %s' % (type(exc).__name__, exc) + ctx) if identity else None
                raise tabword.TabwordError(note or '服务商连接失败', 502) from None
            billing.record(identity, operation)
            handed, slot = slot, None      # 流结束（或断开）时由 capture_stream 归还名额
            release = (lambda: billing.GATE.release(handed)) if handed else None
            return StreamingResponse(billing.capture_stream(identity, operation, stream, release),media_type='text/event-stream',
                                     headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        # 非流式调用放到工作线程里，最多等 deadline() 秒；名额等工作线程真正结束才归还。
        handed, slot = slot, None
        release = (lambda: billing.GATE.release(handed)) if handed else None
        try:
            result = billing.call_with_deadline(lambda: tabword.call(operation,values,content), release, operation + ctx)
        except tabword.TabwordError as exc:
            note = billing.record(identity, operation, error=True, reason=str(exc) + ctx) if identity else None
            advice = billing.note_platform_failure(operation, values, exc)
            raise tabword.TabwordError(' '.join(x for x in (str(exc), note, advice) if x), exc.status) from None
        except Exception as exc:
            note = billing.record(identity, operation, error=True, reason='%s: %s' % (type(exc).__name__, exc) + ctx) if identity else None
            raise tabword.TabwordError(note or '服务商连接失败', 502) from None
        if identity:
            billing.record(identity, operation, result)
            billing.note_platform_success(operation, values)
        return result
    except tabword.TabwordError as exc:
        raise HTTPException(status_code=exc.status,detail=str(exc)) from None
    finally:
        if slot:
            billing.GATE.release(slot)
