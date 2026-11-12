# -*- coding: utf-8 -*-
"""排版前的「识别结果预览 + 编辑」和「AI 助手」。

流程：上传 .docx → analyze（规则识别，免费）→ 用户在预览里改 → （可选）AI 助手扫描 / 对话 →
确认后 api_dp_format 带着 analysisId + overrides 排版。

要点
  * 分析记录单独放在 ANALYSES，不进 JOBS：JOBS 有 KEEP_JOBS 回收、会被 _running_count 计数，
    还会被 /api/job/{id} 暴露，混在一起会误伤真实排版任务。
  * 排版用的是服务端保存的那份字节，下标 idx 才保证和预览一致。
  * AI 只返回「待确认的修改」（role/level），不改文字；按实际 token 计费（先预留后结算，失败全额退）。
  * docx2tex / yaml 在纯 API 镜像里不一定有，所以一律在函数里才导入。
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from core import points
from web.account_routes import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/format/analyze')

ANALYSES = {}
_LOCK = threading.Lock()
_SLOTS = threading.BoundedSemaphore(2)       # 同时最多 2 个分析在跑（纯 Python，但文档大时也吃内存）
TTL = 30 * 60
PER_USER = 3
MAX_AI_CALLS = 12                            # 每份文档最多让 AI 跑多少次
MAX_MESSAGE_CHARS = 500
_ID = re.compile(r'a[0-9a-f]{32}')
CHECKPOINT = 'cpx_analyze'
EXPIRED = '识别结果已过期，请重新检查'


def _max_upload_mb():
    return int(os.environ.get('THESIS_MAX_UPLOAD_MB', '20') or 20)


# ---- 记录管理 ----

def _drop(record):
    shutil.rmtree(record['dir'], ignore_errors=True)


def purge_expired(now=None):
    """删掉超过 TTL 的分析记录和临时目录；由 web/app.py 的定时清理调用。"""
    now = now or time.time()
    with _LOCK:
        for key in [k for k, r in ANALYSES.items() if now - r['created'] > TTL]:
            _drop(ANALYSES.pop(key))


def get_analysis(analysis_id, user_id):
    """取分析记录；不是自己的、不存在、过期都一样返回 404，不透露记录是否存在。"""
    purge_expired()
    rec = ANALYSES.get(analysis_id) if _ID.fullmatch(analysis_id or '') else None
    if not rec or rec['user_id'] != user_id:
        raise HTTPException(404, EXPIRED)
    return rec


def discard(analysis_id):
    with _LOCK:
        record = ANALYSES.pop(analysis_id, None)
    if record:
        _drop(record)


def read_source(record):
    return Path(record['path']).read_bytes()


# ---- AI 配置 ----

def _config():
    try:
        from core import profile
        return profile.load('hkjjxy')
    except ImportError:
        return None


def _manager(cfg):
    from core.dsh import DSHManager
    effective = dict(cfg)
    effective['dsh'] = dict(cfg.get('dsh') or {}, enabled=True)
    return DSHManager(effective)


def _ai_available(cfg):
    if cfg is None:
        return False
    from core.dsh import runner
    return runner.available(cfg) and CHECKPOINT in ((cfg.get('dsh') or {}).get('checkpoints') or [])


def _lines_for(rows):
    from core.dsh import outline
    return outline.analysis_lines(rows)


def _ai_info(rec, rows):
    cfg = _config()
    info = {'available': _ai_available(cfg), 'calls_left': max(0, MAX_AI_CALLS - rec['ai_calls']),
            'tokens': rec['tokens'], 'charged': rec['charged']}
    price = points.ai_pricing()
    lines, truncated = _lines_for(rows)
    chars = sum(len(line) for line in lines) + 1800            # 清单 + 提示词模板
    info.update(rate=points._show(price['rate_units']), min_charge=points._show(price['min_units']),
                reserve=points._show(points.ai_reserve_estimate(chars)), truncated=truncated)
    return info


def _payload(rec, result):
    return {'analysis_id': rec['id'], **result, 'ttl': TTL, 'ai': _ai_info(rec, result['rows'])}


def _fail(exc):
    raise HTTPException(exc.status, str(exc)) from None


# ---- 接口 ----

@router.post('')
def create(file: UploadFile = File(...), user=Depends(current_user)):
    from core import docx2tex
    name = (file.filename or '').lower()
    if name.endswith('.doc') and not name.endswith('.docx'):
        raise HTTPException(400, '识别结果预览暂只支持 .docx：请在 Word 里「另存为 .docx」，或直接点「开始排版」')
    if not name.endswith('.docx'):
        raise HTTPException(400, '请上传 .docx 文件')
    limit = _max_upload_mb() * 1024 * 1024
    data = file.file.read(limit + 1)
    if not data:
        raise HTTPException(400, '上传的文件是空的')
    if len(data) > limit:
        raise HTTPException(413, '文件超过 %d MB，请压缩图片后重试' % _max_upload_mb())
    if not _SLOTS.acquire(blocking=False):
        raise HTTPException(503, '当前识别任务较多，请稍后再试')
    work = tempfile.mkdtemp(prefix='thesis-analysis-')
    try:
        path = Path(work) / 'source.docx'
        path.write_bytes(data)
        try:
            result, items, marks = docx2tex.analyze_items(str(path))
        except Exception:                                  # noqa: BLE001 — 坏文件 / 加密文件 / 非 Word
            logger.exception('analyze failed')
            raise HTTPException(400, '无法读取这份 Word 文档，请确认它是有效的 .docx') from None
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        _SLOTS.release()
    stops = [i for i in (marks.get('ref'), marks.get('ack')) if i is not None]
    record = {'id': 'a' + uuid.uuid4().hex, 'user_id': user['id'], 'dir': work, 'path': str(path),
              'created': time.time(), 'items': items, 'body_start': marks['body_start'],
              'stop': min(stops) if stops else None, 'ai_calls': 0, 'history': [], 'tokens': 0, 'charged': 0,
              'lock': threading.Lock()}
    purge_expired()
    with _LOCK:
        mine = sorted((r for r in ANALYSES.values() if r['user_id'] == user['id']), key=lambda r: r['created'])
        for old in mine[:max(0, len(mine) - PER_USER + 1)]:   # 每人最多 PER_USER 份，挤掉最旧的
            _drop(ANALYSES.pop(old['id']))
        ANALYSES[record['id']] = record
    return _payload(record, result)


class OverridesReq(BaseModel):
    overrides: list = []


@router.get('/{analysis_id}')
def restore(analysis_id: str, user=Depends(current_user)):
    from core import docx2tex
    rec = get_analysis(analysis_id, user['id'])
    return _payload(rec, docx2tex.analyze(rec['path']))


@router.post('/{analysis_id}/preview')
def preview(analysis_id: str, body: OverridesReq, user=Depends(current_user)):
    """按用户的修改重新识别一遍：看到「实际会变成什么」（越级标题被压缩、没图的题注被当成正文…）。"""
    from core import docx2tex
    rec = get_analysis(analysis_id, user['id'])
    return _payload(rec, docx2tex.analyze(rec['path'], body.overrides))


class SimilarReq(BaseModel):
    idx: int


@router.post('/{analysis_id}/similar')
def similar(analysis_id: str, body: SimilarReq, user=Depends(current_user)):
    """找相似行（不用 AI）：同样式 / 同加粗 / 同编号形式 / 字号对齐一致的短行（标题疑似行）。"""
    from core import docx2tex
    rec = get_analysis(analysis_id, user['id'])
    return {'idx': docx2tex.similar_items(rec['items'], body.idx, rec['body_start'], rec['stop'])}


class ChatReq(BaseModel):
    message: str = ''
    overrides: list = []


def _edit_lines(clean):
    labels = {'body': '正文', 'figure_caption': '图题', 'table_caption': '表题'}
    lines = []
    for o in clean:
        what = ('标题%d级' % o['level']) if o.get('role') == 'heading' else labels.get(o.get('role'))
        lines.append('%d | 用户设为%s' % (o['idx'], what) if what else '%d | 用户改了文字' % o['idx'])
    return lines


def _current_state(rows):
    state = {}
    for r in rows:
        if r['kind'] == 'heading':
            state[r['idx']] = ('heading', r['level'])
        elif r['kind'] in ('figure_caption', 'table_caption'):
            state[r['idx']] = (r['kind'], None)
        elif r['kind'] == 'candidate':
            state[r['idx']] = ('body', None)
    return state


def run_ai(rec, user, message, raw_overrides):
    """AI 扫描 / 对话共用：预留积分 → 调模型 → 按实际 token 结算（失败全额退）。"""
    from core import docx2tex
    cfg = _config()
    if not _ai_available(cfg):
        raise HTTPException(503, 'AI 助手暂未启用')
    message = (message or '').strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(422, '单条消息不能超过 %d 字' % MAX_MESSAGE_CHARS)
    if rec['ai_calls'] >= MAX_AI_CALLS:
        raise HTTPException(429, '这份文档的 AI 调用次数已达上限，请重新检查一份')
    if not rec['lock'].acquire(blocking=False):
        raise HTTPException(409, '上一条还在处理，请稍等')
    ref = None
    try:
        clean, _ = docx2tex.normalize_overrides(raw_overrides, rec['items'])
        result = docx2tex.analyze(rec['path'], clean)         # 带上用户已做的修改，AI 看到的是「现在」的样子
        rows = result['rows']
        lines, truncated = _lines_for(rows)
        manager = _manager(cfg)
        if not manager.cost.can_user(user['id']):
            raise HTTPException(429, '今天的 AI 调用次数已用完，明天再试')
        if not manager.cost.can_run('ai_analyze'):
            raise HTTPException(429, 'AI 今日总配额已用完')
        valid = {r['idx'] for r in rows if r['kind'] in ('heading', 'figure_caption', 'table_caption', 'candidate')}
        context = {'structure_lines': lines, 'edit_lines': _edit_lines(clean), 'history': rec['history'],
                   'message': message or None, 'valid_idx': valid, 'current': _current_state(rows),
                   'max_tokens': points.ai_pricing()['max_output']}
        checkpoint = manager.checkpoints.get(CHECKPOINT)
        prompt_chars = len(checkpoint.build_prompt(context)) if checkpoint else sum(map(len, lines))
        rec['ai_calls'] += 1
        ref = 'ai-%s-%d' % (rec['id'], rec['ai_calls'])
        try:
            reserved = points.ai_reserve(user['id'], ref, prompt_chars)   # 余额不足在这里 402，不会调用模型
        except points.PointsError as exc:
            rec['ai_calls'] -= 1
            ref = None
            _fail(exc)
        manager.cost.record_user(user['id'])
        manager.client.reasoning_effort = 'off'                # 分类任务不需要长思考，思考 token 会成倍加钱
        try:
            out = asyncio.run(manager.run_checkpoint(CHECKPOINT, context))
        except Exception:                                      # noqa: BLE001
            logger.exception('AI analyze failed')
            out = None
        if not out or not out.get('success'):
            points.ai_release(ref)
            ref = None
            raise HTTPException(502, 'AI 这次没有给出可用的结果，本次积分已全额退还。你仍然可以手动修改。')
        usage = out.get('usage') or {}
        tokens = usage.get('total') or (prompt_chars + len(out.get('reply', '')))
        settled = points.ai_settle(ref, tokens)
        ref = None
        rec['tokens'] += tokens
        rec['charged'] = round(rec['charged'] + settled['charged'], 1)
        rec['history'] = (rec['history'] + [{'role': 'user', 'content': message or '（扫描整份结构）'},
                                            {'role': 'assistant', 'content': out.get('reply', '')}])[-8:]
        return {'reply': out.get('reply', ''), 'changes': out.get('changes', []),
                'usage': {'prompt': usage.get('prompt', 0), 'completion': usage.get('completion', 0), 'total': tokens},
                'charged': settled['charged'], 'refunded': settled['refunded'], 'reserved': reserved,
                'total_charged': rec['charged'], 'total_tokens': rec['tokens'],
                'calls_left': MAX_AI_CALLS - rec['ai_calls'], 'balance': points.balance(user['id']),
                'truncated': truncated}
    except HTTPException:
        raise
    except BaseException:
        if ref:                                                # 任何意外都不能让预留的积分卡住
            try:
                points.ai_release(ref)
            except Exception:                                  # noqa: BLE001
                logger.exception('ai release failed: %s', ref)
        raise
    finally:
        rec['lock'].release()


@router.post('/{analysis_id}/ai')
def ai_scan(analysis_id: str, body: OverridesReq, user=Depends(current_user)):
    return run_ai(get_analysis(analysis_id, user['id']), user, '', body.overrides)


@router.post('/{analysis_id}/chat')
def ai_chat(analysis_id: str, body: ChatReq, user=Depends(current_user)):
    if not body.message.strip():
        raise HTTPException(422, '请输入内容')
    return run_ai(get_analysis(analysis_id, user['id']), user, body.message, body.overrides)
