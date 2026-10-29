"""Persistent prices, content-bound checkout and single-use processing grants."""
from contextlib import closing
import hashlib
import io
import json
import os
import sqlite3
import time
import uuid
import zipfile
from xml.etree import ElementTree

from core import alipay, tabword

DEFAULTS = {"format_export_cents": 2999, "detect_text_cents": 99, "detect_file_cents": 399, "rewrite_per_1000_cents": 100}
SUBMISSIONS = frozenset({"detect_text", "detect_file", "paraphrase", "paraphrase_stream",
                         "paraphrase_async", "document_paraphrase", "document_async"})
TASKS = {"document_async": "document_task", "paraphrase_async": "paraphrase_task"}


def connect():
    conn = sqlite3.connect(alipay._db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS service_prices (name TEXT PRIMARY KEY, cents INTEGER NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS service_quotes (
        id TEXT PRIMARY KEY, operation TEXT NOT NULL, digest TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, characters INTEGER NOT NULL, created_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'ready', task_id TEXT, task_operation TEXT, error TEXT)""")
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(service_quotes)')}
    for name in ('result_json', 'result_sse'):
        if name not in columns:
            conn.execute(f'ALTER TABLE service_quotes ADD COLUMN {name} TEXT')
    conn.commit()
    return conn


def prices():
    defaults = dict(DEFAULTS)
    configured = os.getenv('THESIS_FORMAT_PRICE_CENTS', '').strip()
    if configured.isdigit() and int(configured) > 0:
        defaults['format_export_cents'] = int(configured)
    with closing(connect()) as conn:
        return {**defaults, **{r['name']: r['cents'] for r in conn.execute("SELECT * FROM service_prices")}}


def save_prices(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise tabword.TabwordError("请提交全部四项服务价格（单位：分）", 422)
    if any(type(v) is not int or not 1 <= v <= 100000 for v in values.values()):
        raise tabword.TabwordError("价格须为 1–100000 分的整数", 422)
    with closing(connect()) as conn, conn:
        conn.executemany("INSERT OR REPLACE INTO service_prices VALUES (?,?)", values.items())
    return prices()


def digest(operation, values, file):
    payload = json.dumps([operation, values, file[0] if file else None], sort_keys=True,
                         ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(payload + b'\0' + (file[1] if file else b'')).hexdigest()


def character_count(values, file):
    text = values.get('text', '')
    if file:
        if not file[0].lower().endswith('.docx'):
            raise tabword.TabwordError("按字数计费的文档改写请上传 .docx 文件", 422)
        try:
            with zipfile.ZipFile(io.BytesIO(file[1])) as archive:
                info = archive.getinfo('word/document.xml')
                if info.file_size > 10 * 1024 * 1024:
                    raise ValueError()
                raw = archive.read(info)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError()
            root = ElementTree.fromstring(raw)
            text = ''.join(node.text or '' for node in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
        except (ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, NotImplementedError):
            raise tabword.TabwordError("无法读取 Word 正文字数，请检查文件", 422) from None
    count = sum(not char.isspace() for char in text)
    if not count:
        raise tabword.TabwordError("正文不能为空", 422)
    return count


def quote(operation, values, file=None):
    if operation not in SUBMISSIONS:
        raise tabword.TabwordError("此服务暂不支持购买", 403)
    tabword.prepare(operation, values, file)
    p = prices()
    count = 0
    if operation in ('detect_text', 'detect_file'):
        amount = p[operation + '_cents']
    else:
        count = character_count(values, file)
        amount = max(1, (count * p['rewrite_per_1000_cents'] + 999) // 1000)
    if amount > 10000000:
        raise tabword.TabwordError("订单金额超过限额，请拆分文档", 422)
    identity = 'TW-' + uuid.uuid4().hex
    with closing(connect()) as conn, conn:
        conn.execute("""INSERT INTO service_quotes
            (id,operation,digest,amount_cents,characters,created_at) VALUES (?,?,?,?,?,?)""",
                     (identity, operation, digest(operation, values, file), amount, count, int(time.time())))
    return {"quote_id": identity, "amount_cents": amount, "characters": count, "prices": p}


def get_quote(identity):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM service_quotes WHERE id=?", (identity,)).fetchone()
    if not row:
        raise tabword.TabwordError("服务订单不存在", 404)
    return dict(row)


def create_payment(identity, referral=None, campaign=''):
    q = get_quote(identity)
    if q['state'] != 'ready':
        raise tabword.TabwordError("该订单已提交处理，请勿再次购买", 409)
    existing = None
    # Reuse a valid checkout even if an administrator has since changed prices.
    if time.time() - q['created_at'] > 15 * 60:
        with closing(alipay._connect()) as conn:
            existing = conn.execute("SELECT out_trade_no FROM payment_orders WHERE job_id=? AND status='TRADE_SUCCESS'", (identity,)).fetchone()
        if not existing:
            raise tabword.TabwordError("报价已过期，请重新计价", 409)
    title = '文本检测' if q['operation'] == 'detect_text' else '文件检测' if q['operation'] == 'detect_file' else '论文改写'
    return alipay.create_order(identity, title, referral, campaign, amount_cents=q['amount_cents'], subject_prefix='良品论文：')


def authorize(operation, values, file, order_id):
    order = alipay.get_order(order_id)
    if not order or not order['job_id'].startswith('TW-') or order['status'] != 'TRADE_SUCCESS':
        raise tabword.TabwordError("请先完成此服务的支付宝付款", 402)
    q = get_quote(order['job_id'])
    if order['amount_cents'] != q['amount_cents']:
        raise tabword.TabwordError("订单金额不匹配", 402)
    if operation in ('document_task', 'paraphrase_task'):
        if q['task_operation'] != operation or q['task_id'] != values.get('task_id') or file:
            raise tabword.TabwordError("任务与支付订单不匹配", 403)
        return q['id']
    if q['operation'] != operation or q['digest'] != digest(operation, values, file):
        raise tabword.TabwordError("内容、文件或处理选项已变化，请重新计价付款", 403)
    with closing(connect()) as conn, conn:
        changed = conn.execute("UPDATE service_quotes SET state='processing' WHERE id=? AND state='ready'", (q['id'],)).rowcount
    if changed != 1:
        raise tabword.TabwordError("订单已经提交处理，不能重复调用；如失败请联系管理员核查", 409)
    return q['id']


def record(identity, operation, result=None, error=None, stream=None):
    if operation in ('document_task', 'paraphrase_task'):
        return
    payload = result if isinstance(result, dict) else {}
    for key in ('data', 'result'):
        if isinstance(payload.get(key), dict):
            payload = payload[key]
    task_id = payload.get('task_id') or payload.get('jobId')
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE service_quotes SET state=?,task_id=?,task_operation=?,error=?,result_json=?,result_sse=? WHERE id=?",
                     ('failed' if error else 'complete' if stream is not None or (result is not None and operation not in TASKS) else 'submitted', str(task_id) if task_id else None,
                      TASKS.get(operation) if task_id else None,
                      '提交异常，需人工核查，禁止自动重试' if error else None,
                      json.dumps(result, ensure_ascii=False) if result is not None and not error else None,
                      stream if not error else None, identity))


def capture_stream(identity, operation, source):
    chunks = []
    size = 0
    try:
        for chunk in source:
            size += len(chunk)
            if size > 10 * 1024 * 1024:
                raise tabword.TabwordError('处理结果过大，请联系管理员核查')
            chunks.append(chunk)
            yield chunk
        stream = b''.join(chunks).decode('utf-8')
        # Provider streams can report failure in an SSE packet with HTTP200.
        failed = False
        done = False
        for packet in stream.replace('\r\n', '\n').split('\n\n'):
            if any(line.strip() == 'event: error' for line in packet.splitlines()):
                failed = True
            if any(line.strip() == 'event: done' for line in packet.splitlines()):
                done = True
        record(identity, operation, error=failed or not done, stream=stream)
    except BaseException:
        record(identity, operation, error=True)
        raise
    finally:
        close = getattr(source, 'close', None)
        if close:
            close()


def admin_orders():
    with closing(alipay._connect()):
        pass
    with closing(connect()) as conn:
        rows = conn.execute("""SELECT q.id, q.operation, q.amount_cents, q.characters,
            q.created_at,q.state,q.error,p.out_trade_no,p.status AS payment_status
            FROM service_quotes q LEFT JOIN payment_orders p ON p.job_id=q.id
            ORDER BY q.created_at DESC LIMIT 100""").fetchall()
    return [dict(row) for row in rows]
