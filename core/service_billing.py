"""Persistent prices, content-bound checkout and single-use processing grants."""
from contextlib import closing
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import uuid
import zipfile
from xml.etree import ElementTree

from core import alipay, tabword

DEFAULTS = {"format_export_cents": 2999, "detect_text_cents": 99, "detect_file_cents": 399, "rewrite_per_1000_cents": 100}
SUBMISSIONS = frozenset({"detect_text", "detect_file", "paraphrase", "paraphrase_stream",
                         "paraphrase_async", "document_paraphrase", "document_async"})
TASKS = {"document_async": "document_task", "paraphrase_async": "paraphrase_task"}
# 文本改写：同一段原文首次全价，之后再改写享受 REWRITE_DISCOUNTS 次半价（不论首次成败）。
TEXT_REWRITES = frozenset({"paraphrase", "paraphrase_stream", "paraphrase_async"})
REWRITE_DISCOUNTS = 3
REWRITE_DISCOUNT_WINDOW = 24 * 3600
logger = logging.getLogger(__name__)


class Gate:
    """同时提交给服务商的任务数上限（服务商的并发上限是 10）。

    满了就立刻拒绝，调用方在订单还是 ready 时返回「请稍后重试」，不会消耗付款。
    每个名额带租期：页面中途关掉、生成器没被正常关闭时，名额会在租期后自动归还。"""

    def __init__(self, limit, lease=20 * 60):
        self.limit, self.lease = limit, lease
        self._held, self._lock = {}, threading.Lock()

    def acquire(self):
        now = time.time()
        with self._lock:
            for token in [t for t, at in self._held.items() if now - at > self.lease]:
                del self._held[token]
            if len(self._held) >= self.limit:
                return None
            token = uuid.uuid4().hex
            self._held[token] = now
            return token

    def release(self, token):
        with self._lock:
            self._held.pop(token, None)

    def busy(self):
        with self._lock:
            return len(self._held)


def _gate_limit():
    value = os.environ.get('TABWORD_MAX_CONCURRENT', '').strip()
    return int(value) if value.isdigit() and int(value) > 0 else 10


GATE = Gate(_gate_limit())


def _seconds(name, default):
    value = os.environ.get(name, '').strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def deadline():
    """一次非流式调用（检测等）最多等服务商多久。"""
    return _seconds('TABWORD_DEADLINE_SECONDS', 180)


def stream_deadline():
    """一次流式改写从开始到结束最多多久。"""
    return _seconds('TABWORD_STREAM_DEADLINE_SECONDS', 300)


def deadlines():
    """各服务的最长等待秒数，页面据此显示倒计时。"""
    normal, streaming = deadline(), stream_deadline()
    return {'detect_text': normal, 'detect_file': normal, 'document_async': normal, 'paraphrase': normal,
            'paraphrase_async': normal, 'paraphrase_stream': streaming}


_WORKERS = ThreadPoolExecutor(max_workers=32, thread_name_prefix='tabword')


def call_with_deadline(fn, on_finished=None, label=''):
    """在工作线程里调用服务商，最多等 deadline() 秒。

    超时就放弃等待并抛 TabwordError(504)；工作线程不能被强杀，会跑到它自己的超时为止。
    on_finished 在工作线程真正结束时才调用（用来归还并发名额：服务商那边还占着）。
    迟到的结果不会写进订单（用户已经被告知失败并退了积分），只记一条日志方便排查。"""
    limit = deadline()
    started = time.time()
    future = _WORKERS.submit(fn)
    if on_finished:
        future.add_done_callback(lambda _f: on_finished())
    try:
        return future.result(timeout=limit)
    except FutureTimeout:
        def late(f):
            try:
                f.result()
                logger.warning("tabword %s: result arrived %d s after the %d s deadline (discarded)",
                               label, time.time() - started, limit)
            except BaseException as exc:                       # noqa: BLE001
                logger.warning("tabword %s: failed %d s after the deadline: %s: %s",
                               label, time.time() - started - limit, type(exc).__name__, exc)
        future.add_done_callback(late)
        raise tabword.TabwordError("服务商在 %d 秒内没有返回结果，已放弃等待" % limit, 504) from None
BUSY_MESSAGE = "当前处理的人数较多，请 30 秒后重试。订单和积分都没有动，不会重复扣费"


def connect():
    conn = sqlite3.connect(alipay._db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS service_prices (name TEXT PRIMARY KEY, cents INTEGER NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS service_quotes (
        id TEXT PRIMARY KEY, operation TEXT NOT NULL, digest TEXT NOT NULL,
        amount_cents INTEGER NOT NULL, characters INTEGER NOT NULL, created_at INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'ready', task_id TEXT, task_operation TEXT, error TEXT)""")
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(service_quotes)')}
    for name in ('result_json', 'result_sse', 'text_key', 'user_id'):
        if name not in columns:
            conn.execute(f'ALTER TABLE service_quotes ADD COLUMN {name} TEXT')
    conn.execute("""CREATE TABLE IF NOT EXISTS platform_status (
        platform TEXT PRIMARY KEY, disabled INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual', until REAL, updated_at REAL NOT NULL)""")
    for name in ('started_at', 'elapsed'):
        if name not in columns:
            conn.execute(f'ALTER TABLE service_quotes ADD COLUMN {name} REAL')
    if 'discounted' not in columns:
        conn.execute('ALTER TABLE service_quotes ADD COLUMN discounted INTEGER NOT NULL DEFAULT 0')
    conn.execute('CREATE INDEX IF NOT EXISTS service_quotes_by_text ON service_quotes(user_id, text_key)')
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


_PRESETS = {'at': 0, 'codes': None}


def official_presets():
    """官方现有的改写模型代码（缓存 10 分钟）。读不到（服务商故障、未配置）返回 None，调用方就不拦。"""
    if os.environ.get('TABWORD_VALIDATE_PRESETS', 'true').lower() == 'false':
        return None
    if time.time() - _PRESETS['at'] < 600:
        return _PRESETS['codes']
    try:
        data = tabword.read_catalog('presets', {})
        codes = {p.get('code') for p in data.get('presets', []) if isinstance(p, dict) and p.get('code')}
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("preset catalog unavailable, not validating: %s", exc)
        from core import alerts
        alerts.alert(('catalog',), '读取服务商改写模型目录失败', '错误：%s\n\n暂时不校验用户选的改写模型。' % exc)
        codes = None
    _PRESETS.update(at=time.time(), codes=codes or None)
    return _PRESETS['codes']


def check_preset(operation, values):
    """报价前拦下已下线的改写模型：不然用户付了款，服务商才回「预设不存在」。"""
    if operation not in TEXT_REWRITES | {'document_async', 'document_paraphrase'} or not isinstance(values, dict):
        return
    codes = official_presets()
    if not codes:
        return
    for key in ('preset', 'preset_zh', 'preset_en'):
        value = values.get(key)
        if value and value not in codes:
            raise tabword.TabwordError("所选改写模型已下线或不存在，请刷新页面后重新选择", 422)


DETECT_OPERATIONS = frozenset({'detect_text', 'detect_file'})
# 检测平台是否可用是动态的，以实际报错为准：
#   · 服务商明确说「预设不存在」→ 马上暂停；其他接口类错误（5xx、超时、连接中断）→ 窗口内连续出错才暂停。
#   · 暂停是短时间的，到点自动放行去试；一次成功就恢复；用户自己的问题（4xx，例如文件为空）不算平台的错。
AUTO_PAUSE_SECONDS = _seconds('TABWORD_PAUSE_SECONDS', 180)
FAIL_WINDOW = 10 * 60
FAIL_THRESHOLD = 2
_PLATFORM_FAILS, _PLATFORM_LOCK = {}, threading.Lock()


def _platform_label(platform):
    return re.sub(r'_(zh|en)$', '', str(platform), flags=re.I)


def _busy_message(platform):
    return "「%s」平台接口暂时不可用，请稍候重试，或选择其他检测平台" % _platform_label(platform)


def unavailable_platforms():
    """现在暂时不能用的检测平台 {平台: 原因}。自动暂停的到点自动放行；管理员手动停用的一直生效到手动恢复。"""
    now = time.time()
    with closing(connect()) as conn:
        rows = conn.execute("SELECT platform, reason FROM platform_status WHERE disabled=1 AND (until IS NULL OR until>?)",
                            (now,)).fetchall()
    return {r['platform']: r['reason'] for r in rows}


def set_platform(platform, disabled, reason='', source='manual', seconds=None):
    platform = str(platform or '').strip()
    if not platform or len(platform) > 40:
        raise tabword.TabwordError("请提供检测平台名称", 422)
    until = time.time() + seconds if disabled and seconds else None
    with closing(connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO platform_status VALUES (?,?,?,?,?,?)",
                     (platform, int(bool(disabled)), reason[:200], source, until, time.time()))
    if not disabled:
        with _PLATFORM_LOCK:
            _PLATFORM_FAILS.pop(platform, None)
    return unavailable_platforms()


def check_platform(operation, values):
    """报价前拦下暂时不可用的检测平台，提醒用户稍后重试或换平台，不让他付款后才失败。"""
    if operation not in DETECT_OPERATIONS or not isinstance(values, dict):
        return
    platform = str(values.get('task_platform') or '')
    if platform in unavailable_platforms():
        raise tabword.TabwordError(_busy_message(platform), 409)


def _platform_of(operation, values):
    if operation not in DETECT_OPERATIONS or not isinstance(values, dict):
        return ''
    return str(values.get('task_platform') or '')


def note_platform_failure(operation, values, exc):
    """检测失败后记账。接口类错误会提醒用户稍后重试；连续出错或「预设不存在」会暂停该平台一小会儿。返回给用户的提醒。"""
    platform = _platform_of(operation, values)
    message, status = str(exc), getattr(exc, 'status', 502)
    certain = '预设不存在' in message
    if not platform or not (certain or status >= 500):
        return ''                                              # 用户自己的问题（文件为空等）不怪平台
    now = time.time()
    with _PLATFORM_LOCK:
        recent = [t for t in _PLATFORM_FAILS.get(platform, []) if now - t < FAIL_WINDOW] + [now]
        _PLATFORM_FAILS[platform] = recent
    if certain or len(recent) >= FAIL_THRESHOLD:
        set_platform(platform, True, '接口连续报错，暂时不可用', 'auto', AUTO_PAUSE_SECONDS)
        logger.warning("detection platform %s paused for %d s after %d recent failures: %s",
                       platform, AUTO_PAUSE_SECONDS, len(recent), message)
        from core import alerts
        alerts.alert(('platform', platform), '检测平台「%s」接口不可用，已暂停 %d 分钟' % (_platform_label(platform), AUTO_PAUSE_SECONDS // 60),
                     '平台：%s\n最近 %d 分钟内出错 %d 次\n最后一次错误：%s\n\n暂停期间用户看到「接口暂时不可用，请稍候重试」；'
                     '到点会自动放行，再出错会再暂停。服务商修好后无需操作。'
                     % (platform, FAIL_WINDOW // 60, len(recent), message[:300]))
        return _busy_message(platform) + "。已暂停该平台，稍后会自动恢复。"
    return _busy_message(platform) + "。"


def note_platform_success(operation, values):
    """检测成功：清掉连续失败的记录；如果它之前被自动暂停，立刻恢复。"""
    platform = _platform_of(operation, values)
    if not platform:
        return
    with _PLATFORM_LOCK:
        _PLATFORM_FAILS.pop(platform, None)
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE platform_status SET disabled=0 WHERE platform=? AND source='auto'", (platform,))


def text_key(values):
    """文本改写的「同一段原文」：只看文字本身（去掉空白），和预设无关，换个预设重改也算。"""
    text = ''.join(str(values.get('text', '')).split()) if isinstance(values, dict) else ''
    return hashlib.sha256(text.encode()).hexdigest() if text else None


def _paid_quotes(conn, user_id, key):
    """该用户这段原文已付款的文本改写报价（最早在前）。"""
    since = int(time.time()) - REWRITE_DISCOUNT_WINDOW
    ids = [r['id'] for r in conn.execute("""SELECT id FROM service_quotes WHERE user_id=? AND text_key=?
                                            AND created_at>=? ORDER BY created_at""", (str(user_id), key, since))]
    return [i for i in ids if alipay.job_paid(i)]


def rewrite_offer(user_id, values):
    """这段原文再改写还剩几次半价。返回 {"paid": 已付款次数, "discounts_left": 剩余半价次数}。"""
    key = text_key(values)
    if not user_id or not key:
        return {"paid": 0, "discounts_left": 0}
    with closing(connect()) as conn:
        paid = _paid_quotes(conn, user_id, key)
        marks = ','.join('?' * len(paid)) or 'NULL'
        used = conn.execute(f"SELECT COUNT(*) FROM service_quotes WHERE id IN ({marks}) AND discounted=1",
                            paid).fetchone()[0]
    left = REWRITE_DISCOUNTS - used if paid else 0
    return {"paid": len(paid), "discounts_left": max(0, left)}


def quote(operation, values, file=None, user_id=None):
    if operation not in SUBMISSIONS:
        raise tabword.TabwordError("此服务暂不支持购买", 403)
    tabword.prepare(operation, values, file)
    check_preset(operation, values)
    check_platform(operation, values)
    p = prices()
    count = 0
    discounted = False
    key = text_key(values) if operation in TEXT_REWRITES and not file else None
    if operation in ('detect_text', 'detect_file'):
        amount = p[operation + '_cents']
    else:
        count = character_count(values, file)
        amount = max(1, (count * p['rewrite_per_1000_cents'] + 999) // 1000)
        if key and user_id and rewrite_offer(user_id, values)["discounts_left"] > 0:
            amount, discounted = max(1, (amount + 1) // 2), True
    if amount > 10000000:
        raise tabword.TabwordError("订单金额超过限额，请拆分文档", 422)
    identity = 'TW-' + uuid.uuid4().hex
    with closing(connect()) as conn, conn:
        conn.execute("""INSERT INTO service_quotes
            (id,operation,digest,amount_cents,characters,created_at,text_key,user_id,discounted) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (identity, operation, digest(operation, values, file), amount, count, int(time.time()),
                      key, str(user_id) if user_id else None, int(discounted)))
    offer = rewrite_offer(user_id, values) if key else {"paid": 0, "discounts_left": 0}
    return {"quote_id": identity, "amount_cents": amount, "characters": count, "prices": p,
            "discounted": discounted, "discounts_left": offer["discounts_left"] - (1 if discounted else 0),
            "paid_before": offer["paid"],
            "rewrite_discounts": REWRITE_DISCOUNTS if key else 0}


EXPIRED = "付款已超过 24 小时，结果已按规定删除"


def get_quote(identity):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM service_quotes WHERE id=?", (identity,)).fetchone()
    if not row:
        raise tabword.TabwordError("服务订单不存在", 404)
    row = dict(row)
    # Enforce the retention limit on every read, not only when the sweeper runs.
    if row['state'] != 'expired' and alipay.result_expired(identity):
        _expire(identity)
        row.update(state='expired', result_json=None, result_sse=None)
    return row


def _expire(identity):
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE service_quotes SET state='expired', result_json=NULL, result_sse=NULL WHERE id=?",
                     (identity,))


def purge_expired():
    """删除付款满 RESULT_TTL 的改写 / 检测结果；订单和金额记录保留"""
    cutoff = int(time.time()) - alipay.RESULT_TTL
    with closing(alipay._connect()) as conn:
        ids = [r[0] for r in conn.execute("""SELECT DISTINCT job_id FROM payment_orders WHERE job_id LIKE 'TW-%'
            AND status='TRADE_SUCCESS' AND COALESCE(paid_at, created_at) < ?""", (cutoff,))]
    with closing(connect()) as conn:
        live = {r[0] for r in conn.execute("SELECT id FROM service_quotes WHERE state!='expired'")}
    for identity in ids:
        if identity in live:
            _expire(identity)
    fail_stuck()


STUCK_AFTER = 10 * 60          # 正常最长约 5 分钟；超过 10 分钟还在「处理中」就是中断了
LEGACY_STUCK_AFTER = 60 * 60    # 旧订单没有开始时间，用付款时间估算，宽松一些，免得误伤
STUCK_STREAMS = ('paraphrase_stream', 'paraphrase')


def fail_stuck(now=None):
    """把中断、卡死的订单标记失败并退积分：页面关掉、服务重启、连接断开都会留下这种订单。

    处理中的异步文档任务（submitted）不算卡住，它们本来就要等服务商。返回处理的订单号。"""
    now = now or time.time()
    with closing(connect()) as conn:
        rows = conn.execute("""SELECT id, operation, state, started_at FROM service_quotes
            WHERE state='processing' OR (state='submitted' AND operation IN (%s))"""
                            % ','.join('?' * len(STUCK_STREAMS)), STUCK_STREAMS).fetchall()
    fixed = []
    for row in rows:
        began, limit = row['started_at'], STUCK_AFTER
        if began is None:
            began, limit = alipay.paid_at(row['id']), LEGACY_STUCK_AFTER
        if began is None or now - float(began) < limit:
            continue
        reason = '处理中超过 %d 分钟没有结果，判定为中断（页面关闭、网络断开或服务重启）' % (limit // 60)
        with closing(connect()) as conn, conn:
            claimed = conn.execute("UPDATE service_quotes SET state='failed', error=? WHERE id=? AND state=?",
                                   (reason, row['id'], row['state'])).rowcount
        if claimed != 1:
            continue                                # 刚好在这一刻完成了，别动它
        from core import points
        try:
            refunded = points.refund_spend(row['id'], "%s，积分已全额退还" % _TITLES.get(row['operation'], '服务处理'))
        except Exception:                           # noqa: BLE001
            logger.exception("refund failed for stuck order %s", row['id'])
            refunded = None
        logger.warning("stuck tabword order %s (%s) failed by sweeper; refunded=%s", row['id'], row['operation'], refunded)
        fixed.append(row['id'])
    if fixed:
        from core import alerts
        alerts.alert(('stuck',), '%d 笔订单处理中断，已标记失败' % len(fixed),
                     '这些订单长时间停在「处理中」（页面关闭、网络断开或服务重启）：\n%s\n\n用积分支付的已自动退还。'
                     % '\n'.join(fixed))
    return fixed


def create_payment(identity, referral=None, campaign='', resume_return=False):
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
    return alipay.create_order(identity, title, referral, campaign, amount_cents=q['amount_cents'], subject_prefix='良品论文：',
                               resume_return=resume_return)


def authorize(operation, values, file, order_id):
    order = alipay.get_order(order_id)
    if not order or not order['job_id'].startswith('TW-') or order['status'] != 'TRADE_SUCCESS':
        raise tabword.TabwordError("请先完成此服务的支付宝付款", 402)
    q = get_quote(order['job_id'])
    if q['state'] == 'expired':
        raise tabword.TabwordError(EXPIRED, 410)
    if order['amount_cents'] != q['amount_cents']:
        raise tabword.TabwordError("订单金额不匹配", 402)
    if operation in ('document_task', 'paraphrase_task'):
        if q['task_operation'] != operation or q['task_id'] != values.get('task_id') or file:
            raise tabword.TabwordError("任务与支付订单不匹配", 403)
        return q['id']
    if q['operation'] != operation or q['digest'] != digest(operation, values, file):
        raise tabword.TabwordError("内容、文件或处理选项已变化，请重新计价付款", 403)
    with closing(connect()) as conn, conn:
        changed = conn.execute("UPDATE service_quotes SET state='processing', started_at=? WHERE id=? AND state='ready'",
                             (time.time(), q['id'])).rowcount
    if changed != 1:
        raise tabword.TabwordError("订单已经提交处理，不能重复调用；如失败请联系管理员核查", 409)
    return q['id']


FAILED_REFUNDED = "处理失败，积分已全额退还，可以重新提交"
FAILED_MANUAL = "处理失败；如已付款请保留订单号联系客服退款，可以重新提交"


def record(identity, operation, result=None, error=None, stream=None, reason=''):
    """记下处理结果。error 为真时标记失败、写入原因，并把积分原额退回。返回失败提示（成功返回 None）。"""
    if not identity or operation in ('document_task', 'paraphrase_task'):
        return None
    payload = result if isinstance(result, dict) else {}
    for key in ('data', 'result'):
        if isinstance(payload.get(key), dict):
            payload = payload[key]
    task_id = payload.get('task_id') or payload.get('jobId')
    note = None
    if error:
        reason = str(reason or (error if isinstance(error, str) else '') or '服务商未返回完整结果')[:300]
        logger.warning("tabword %s failed for %s: %s", operation, identity, reason)
        from core import points
        refunded = None
        if identity:
            try:
                refunded = points.refund_spend(identity, "%s失败，积分已全额退还" % _TITLES.get(operation, '服务处理'))
            except Exception:                                    # noqa: BLE001  退款失败不能吞掉原错误
                logger.exception("points refund failed for %s", identity)
        note = FAILED_REFUNDED if refunded is not None else FAILED_MANUAL
        reason = '%s（%s）' % (reason, '积分已退' if refunded is not None else '未用积分或已退款，需人工核查')
        from core import alerts
        alerts.alert(('provider', operation), '%s失败' % _TITLES.get(operation, operation),
                     '订单：%s\n原因：%s\n%s' % (identity, reason,
                                         '已自动退还积分。' if refunded is not None
                                         else '没有用积分支付（或已退过），如是支付宝付款请人工退款。'))
    with closing(connect()) as conn, conn:
        conn.execute("UPDATE service_quotes SET state=?,task_id=?,task_operation=?,error=?,result_json=?,result_sse=? WHERE id=?",
                     ('failed' if error else 'complete' if stream is not None or (result is not None and operation not in TASKS) else 'submitted', str(task_id) if task_id else None,
                      TASKS.get(operation) if task_id else None,
                      reason[:400] if error else None,
                      json.dumps(result, ensure_ascii=False) if result is not None and not error else None,
                      stream if not error else None, identity))
        conn.execute("UPDATE service_quotes SET elapsed=? - started_at WHERE id=? AND started_at IS NOT NULL",
                     (time.time(), identity))
    return note


_TITLES = {'paraphrase_stream': '文本改写', 'paraphrase': '文本改写', 'paraphrase_async': '文本改写',
           'document_async': '文档改写', 'document_paraphrase': '文档改写',
           'detect_text': '文本检测', 'detect_file': '文件检测'}


def _stream_failure(stream):
    """从 SSE 文本里找失败原因；成功（有 done 且没有 error）返回 None。"""
    error_message, done, progress = None, False, 0
    for packet in stream.replace('\r\n', '\n').split('\n\n'):
        lines = packet.splitlines()
        event = next((l[6:].strip() for l in lines if l.startswith('event:')), '')
        data = '\n'.join(l[5:].lstrip() for l in lines if l.startswith('data:'))
        if event == 'error':
            try:
                parsed = json.loads(data)
                error_message = str(parsed.get('message') or parsed.get('detail') or parsed) if isinstance(parsed, dict) else str(parsed)
            except ValueError:
                error_message = data or 'event: error'
        elif event == 'done':
            done = True
        elif event == 'progress':
            progress += 1
    if error_message:
        return '服务商返回错误：%s（已收到 %d 段）' % (error_message[:200], progress)
    if not done:
        return '服务商的流在完成前结束（已收到 %d 段，没有 done 事件）' % progress
    return None


def _sse(event, data):
    return ('event: %s\ndata: %s\n\n' % (event, json.dumps(data, ensure_ascii=False))).encode()


def capture_stream(identity, operation, source, release=None):
    chunks = []
    size = 0
    started = time.time()
    limit = stream_deadline()
    try:
        for chunk in source:
            size += len(chunk)
            if size > 10 * 1024 * 1024:
                raise tabword.TabwordError('处理结果过大，请联系管理员核查')
            chunks.append(chunk)
            yield chunk
            if time.time() - started > limit:
                reason = '服务商处理超过 %d 秒仍未完成，已放弃等待（已传 %d 字节）' % (limit, size)
                note = record(identity, operation, error=True, reason=reason)
                yield _sse('error', {'message': reason})
                if note:
                    yield _sse('refund', {'message': note})
                return
        stream = b''.join(chunks).decode('utf-8', 'replace')
        # Provider streams can report failure in an SSE packet with HTTP200.
        reason = _stream_failure(stream)
        if reason:
            reason += '，耗时 %d 秒' % (time.time() - started)
        note = record(identity, operation, error=bool(reason), stream=stream, reason=reason)
        if note:
            yield _sse('refund', {'message': note})
    except GeneratorExit:
        # 用户关掉页面或网络断开：服务商那边可能还在处理，照样按失败退款。
        record(identity, operation, error=True,
               reason='浏览器连接在完成前断开（已传 %d 字节，耗时 %d 秒）' % (size, time.time() - started))
        raise
    except BaseException as exc:
        record(identity, operation, error=True,
               reason='%s: %s（已传 %d 字节，耗时 %d 秒）' % (type(exc).__name__, exc, size, time.time() - started))
        raise
    finally:
        if release:
            release()
        close = getattr(source, 'close', None)
        if close:
            close()


def admin_orders():
    with closing(alipay._connect()):
        pass
    with closing(connect()) as conn:
        rows = conn.execute("""SELECT q.id, q.operation, q.amount_cents, q.characters,
            q.created_at,q.state,q.error,q.started_at,q.elapsed,p.out_trade_no,p.status AS payment_status,p.payment_product,
            p.affiliate_id,p.affiliate_code,p.affiliate_campaign,p.affiliate_bps,
            a.name AS affiliate_name,a.email AS affiliate_email,
            m.amount_cents AS commission_cents,m.status AS commission_status
            FROM service_quotes q LEFT JOIN payment_orders p ON p.job_id=q.id
            LEFT JOIN affiliates a ON a.id=p.affiliate_id
            LEFT JOIN affiliate_commissions m ON m.order_no=p.out_trade_no
            ORDER BY q.created_at DESC LIMIT 100""").fetchall()
        # Thesis-format Word exports are paid orders too; list them so a customer's
        # "paid but nothing happened" can be looked up by order number.
        exports = conn.execute("""SELECT p.job_id AS id, 'format_export' AS operation, p.amount_cents,
            0 AS characters, p.created_at,
            CASE WHEN p.status='TRADE_SUCCESS' THEN 'paid' ELSE 'ready' END AS state, NULL AS error,
            p.out_trade_no, p.status AS payment_status, p.payment_product,
            p.affiliate_id,p.affiliate_code,p.affiliate_campaign,p.affiliate_bps,
            a.name AS affiliate_name,a.email AS affiliate_email,
            m.amount_cents AS commission_cents,m.status AS commission_status
            FROM payment_orders p
            LEFT JOIN affiliates a ON a.id=p.affiliate_id
            LEFT JOIN affiliate_commissions m ON m.order_no=p.out_trade_no
            WHERE p.job_id NOT LIKE 'TW-%'
            ORDER BY p.created_at DESC LIMIT 100""").fetchall()
    merged = sorted([dict(row) for row in rows] + [dict(row) for row in exports],
                    key=lambda row: row['created_at'], reverse=True)
    return merged[:100]
