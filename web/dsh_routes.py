# -*- coding: utf-8 -*-
"""DSH 调试页的后端：看 DSH 识别的目录、列 skill、手动跑检查点、和 DSH 对话。

所有接口要求登录，且只能看/用自己的排版任务。对话和手动检查会真调 DeepSeek，
所以受两层限额：全局日配额 + 每用户每日次数（dsh.chat.per_user_daily）。
模型只会收到结构清单，正文只给字数——和排版流程里 CP1 的隐私约束一致。
"""
import asyncio
import logging
import os
import re
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import accounts
from core.dsh import DSHManager, outline, runner, skills
from core.dsh.cost_control import CostController
from web.account_routes import current_user

logger = logging.getLogger(__name__)


def page_enabled():
    """调试页总开关：THESIS_DSH_PAGE=true 才对外开放，默认关（部署时不暴露）。"""
    return os.environ.get('THESIS_DSH_PAGE', '').strip().lower() in ('1', 'true', 'yes', 'on')


def _gate():
    if not page_enabled():
        raise HTTPException(404, 'Not Found')


router = APIRouter(prefix='/api/dsh', dependencies=[Depends(_gate)])

SCHOOL = 'hkjjxy'
MAX_MESSAGE_CHARS = 500
MAX_HISTORY = 6
_JOB_ID = re.compile(r'j[0-9a-f]{32}')

SYSTEM_PROMPT = (
    '你是论文排版系统里的「结构助手」DSH，帮用户理解系统对他这篇论文章节目录的识别结果。\n'
    '你只能看到结构清单：标题的文字、层级、原编号，以及每段正文的字数；你看不到任何正文内容。\n'
    '规则：只讨论标题层级、编号、章节结构；不要改写、润色或编造正文；'
    '用户问到看不到的内容就直说看不到；回答用中文，简短具体。'
)

_get_job = None


def bind(get_job):
    """由 web/app.py 注入任务查询函数（避免反向 import app 造成重复加载）。"""
    global _get_job
    _get_job = get_job


def _config():
    # profile 依赖 PyYAML；纯 API 镜像不装它，所以用到时才导入，导入不了就如实告知
    try:
        from core import profile
        return profile.load(SCHOOL)
    except ImportError:
        raise HTTPException(503, 'DSH 需要本地排版运行环境，当前部署未包含')


def _dsh_cfg(cfg):
    return cfg.get('dsh') or {}


def _owned_outline(user, job_id):
    """返回 (job, outline)；不是自己的任务一律 404，不透露任务是否存在。"""
    if not _JOB_ID.fullmatch(job_id or '') or _get_job is None:
        raise HTTPException(404, '排版任务不存在')
    if not accounts.get_upload(user['id'], job_id):
        raise HTTPException(404, '排版任务不存在')
    job = _get_job(job_id)
    if not job or job.get('kind') != 'school' or not job.get('tmp'):
        raise HTTPException(404, '排版任务不存在')
    data = runner.load_outline(job['tmp'])
    if data is None:
        raise HTTPException(404, '该任务没有 DSH 结构记录（只有本地排版的新任务才有）')
    return job, data


def _require_dsh(cfg):
    if not runner.available(cfg):
        raise HTTPException(503, 'DSH 未启用或缺少 DeepSeek Key')


def _manager(cfg):
    # 把生效的开关喂给 manager，避免它自己读 yaml 得到相反结论（同 runner.run_preprocess）
    effective = dict(cfg)
    effective['dsh'] = dict(_dsh_cfg(cfg), enabled=True)
    return DSHManager(effective)


def _quota(cost, user):
    stats = cost.get_stats()['today']
    return {'daily_remaining': stats['remaining'], 'daily_quota': stats['quota'],
            'user_used': cost.user_used(user['id']), 'user_limit': cost.per_user_daily}


@router.get('/skills')
def list_skills(user=Depends(current_user)):
    cfg = _config()
    dsh = _dsh_cfg(cfg)
    enabled = set(dsh.get('checkpoints') or [])
    cost = CostController(dsh)
    return {
        'available': runner.available(cfg),
        'skills': [{'dir': s['dir'], 'name': s['name'], 'description': s['description'],
                    'version': s['version'], 'checkpoint': s['checkpoint'],
                    'enabled': s['checkpoint'] in enabled and s['has_checkpoint']}
                   for s in skills.discover()],
        'quota': _quota(cost, user),
        'max_message_chars': MAX_MESSAGE_CHARS,
    }


@router.get('/jobs')
def list_jobs(user=Depends(current_user)):
    """当前用户有 DSH 结构记录的排版任务（新到旧），供页面选择。"""
    out = []
    for row in accounts.uploads(user['id']):
        if row['kind'] != 'format' or not _JOB_ID.fullmatch(row['ref']):
            continue
        try:
            job = _get_job(row['ref']) if _get_job else None
        except HTTPException:                      # 付款满 24 小时已删除
            continue
        if job and job.get('kind') == 'school' and job.get('tmp') and runner.load_outline(job['tmp']):
            out.append({'job_id': row['ref'], 'name': row['name'], 'created_at': row['created_at']})
    return {'jobs': out}


@router.get('/outline')
def get_outline(job_id: str, user=Depends(current_user)):
    job, data = _owned_outline(user, job_id)
    return {'job_id': job_id, 'name': job.get('name') or '',
            'rows': data['rows'], 'truncated': bool(data.get('truncated')),
            'word_outline': data.get('word_outline') or [],
            'suspicious': bool(data.get('suspicious')),
            'model_input': outline.format_lines(data['rows'], bool(data.get('truncated')))}


class RunReq(BaseModel):
    job_id: str
    checkpoint: str


def _context(job, data):
    rows = data['rows']
    return {
        'source': str(job['tmp']), 'work_dir': str(job['tmp']),
        'meta': {'title': job.get('name') or ''},
        'suspicious': True,   # 手动运行就是要跑，不看规则层有没有存疑
        'structure_lines': outline.format_lines(rows, bool(data.get('truncated'))),
        'outline_lines': data.get('word_outline') or [],
    }


@router.post('/run')
def run_checkpoint(req: RunReq, user=Depends(current_user)):
    cfg = _config()
    _require_dsh(cfg)
    job, data = _owned_outline(user, req.job_id)
    manager = _manager(cfg)
    if req.checkpoint not in manager.checkpoints:
        raise HTTPException(404, '没有这个检查点')
    if not manager.checkpoints[req.checkpoint].is_enabled():
        raise HTTPException(409, '该检查点未启用')
    if not manager.cost.can_user(user['id']):
        raise HTTPException(429, '今天的 DSH 调用次数已用完，明天再试')
    manager.cost.record_user(user['id'])
    started = time.time()
    result = asyncio.run(manager.run_checkpoint(req.checkpoint, _context(job, data), req.job_id))
    if result is None:
        raise HTTPException(409, '该检查点此刻不需要运行')
    if result.get('reason') == 'quota_exceeded':
        raise HTTPException(429, 'DSH 配额已用完')
    return {'success': bool(result.get('success')), 'reason': result.get('reason'),
            'summary': result.get('summary') or '', 'suggestions': result.get('suggestions') or [],
            'warnings': result.get('warnings') or [], 'quality_score': result.get('quality_score'),
            'tokens': result.get('cost', 0), 'elapsed': round(time.time() - started, 2),
            'quota': _quota(manager.cost, user)}


class ChatTurn(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    job_id: str
    message: str
    history: list[ChatTurn] = []


def _prompt(data, message, history):
    lines = outline.format_lines(data['rows'], bool(data.get('truncated')))
    parts = ['【论文结构清单（序号 | 层级 | 文字 | 原编号 | 是否有正文）】', '\n'.join(lines)]
    if data.get('word_outline'):
        parts += ['【Word 原稿的大纲级别】', '\n'.join(data['word_outline'])]
    turns = []
    for turn in history[-MAX_HISTORY:]:
        if turn.role in ('user', 'assistant') and turn.content.strip():
            who = '用户' if turn.role == 'user' else 'DSH'
            turns.append('%s：%s' % (who, turn.content.strip()[:MAX_MESSAGE_CHARS]))
    if turns:
        parts += ['【此前的对话】', '\n'.join(turns)]
    parts += ['【用户现在的问题】', message]
    return '\n\n'.join(parts)


@router.post('/chat')
def chat(req: ChatReq, user=Depends(current_user)):
    cfg = _config()
    _require_dsh(cfg)
    message = (req.message or '').strip()
    if not message:
        raise HTTPException(422, '请输入内容')
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(422, '单条消息不能超过 %d 字' % MAX_MESSAGE_CHARS)
    job, data = _owned_outline(user, req.job_id)
    manager = _manager(cfg)
    if not manager.cost.can_user(user['id']):
        raise HTTPException(429, '今天的 DSH 调用次数已用完，明天再试')
    if not manager.cost.can_run('chat'):
        raise HTTPException(429, 'DSH 今日总配额已用完')
    prompt = _prompt(data, message, req.history)
    manager.cost.record_user(user['id'])
    started = time.time()
    try:
        result = asyncio.run(manager.client.chat(prompt, system=SYSTEM_PROMPT,
                                                 temperature=0.3, use_cache=False))
    except Exception as exc:                      # noqa: BLE001
        # 异常文字可能带上游响应正文，只记日志，不回给用户
        logger.warning("DSH chat failed: %s", exc)
        raise HTTPException(502, 'DSH 暂时无法回答，请稍后再试')
    tokens = int((result.get('usage') or {}).get('total_tokens', 0) or 0)
    manager.cost.record('chat', None, tokens)
    return {'reply': (result.get('content') or '').strip(), 'tokens': tokens,
            'quota': _quota(manager.cost, user),
            'debug': {'model': manager.client.model, 'prompt_chars': len(prompt),
                      'structure_rows': len(data['rows']),
                      'elapsed': round(time.time() - started, 2)}}
