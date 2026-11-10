# -*- coding: utf-8 -*-
"""把 DSH 接进同步的排版流程。

排版流水线是同步的（schoolformat.run 在后台线程里），DSH 是 async。
这里负责两件事：在独立的 event loop 里跑检查点、把结果翻译成人话。

关键约束：**任何情况下都不能因为 DSH 失败而让排版失败**。
配置没开、没 key、超时、模型乱答——一律降级成「跳过」。
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _env_flag(name):
    """环境变量三态：未设置 → None；设置 → True/False"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def enabled(cfg):
    """DSH 总开关：profile.yaml 为准，环境变量 DSH_ENABLED 可覆盖。

    `.env.example` 里一直写着 DSH_ENABLED，但代码之前只读 yaml，
    照 `DSH_ENABLED=false` 配置的人以为关掉了、其实没关。这里补上。
    """
    override = _env_flag('DSH_ENABLED')
    if override is not None:
        return override
    return bool(((cfg or {}).get('dsh') or {}).get('enabled'))


def available(cfg):
    """DSH 是否配置齐了。缺 key 时不初始化，省得每次打 warning。"""
    if not enabled(cfg):
        return False
    dsh_cfg = (cfg or {}).get('dsh') or {}
    if os.getenv('DEEPSEEK_API_KEY', '').strip():
        return True
    api = dsh_cfg.get('api') or {}
    if (api.get('api_key') or '').strip():
        return True
    key_file = (os.getenv('DEEPSEEK_API_KEY_FILE')
                or api.get('api_key_file') or '').strip()
    if key_file:
        from pathlib import Path
        return Path(key_file).is_file()
    return False


def run_preprocess(cfg, context, job_id=None):
    """同步调用 CP1。返回结构化的建议 dict，或 None（没跑/失败/无价值）。"""
    if not available(cfg):
        return None
    try:
        from . import DSHManager
        # 把生效的开关值喂给 manager，避免它自己去读 yaml 得到相反的结论
        effective = dict(cfg or {})
        effective['dsh'] = dict((cfg or {}).get('dsh') or {}, enabled=True)
        manager = DSHManager(effective)
        if not manager.enabled or manager.client is None:
            return None
        result = asyncio.run(manager.run_checkpoint('cp1_preprocess', context, job_id))
    except Exception as exc:                      # noqa: BLE001 — 绝不冒泡到排版
        logger.warning("DSH preprocess skipped: %s", exc)
        return None

    if not result or not result.get('success'):
        return None
    issues = result.get('suggestions') or []
    if not issues:
        return None
    return {
        'issues': issues,
        'notes': result.get('warnings') or [],
        'summary': result.get('summary') or '',
        'quality_score': result.get('quality_score'),
        'cost': result.get('cost', 0),
    }


def to_messages(report, limit=3):
    """把建议翻成结果页上给用户看的中文短句（最多 limit 条）。"""
    if not report:
        return []
    out = []
    for issue in report['issues'][:limit]:
        msg = issue.get('message') or ''
        hint = issue.get('suggestion') or ''
        if msg and hint and hint != msg:
            out.append('结构检查：%s（%s）' % (msg, hint))
        elif msg:
            out.append('结构检查：%s' % msg)
    for note in (report.get('notes') or [])[:2]:
        out.append('结构检查：%s' % note)
    return out


OUTLINE_FILE = 'dsh_outline.json'


def save_outline(work_dir, stat):
    """把 DSH 实际看到的结构落盘，供调试页事后查看。只存结构清单，不存正文。

    任何失败都吞掉：这是调试辅助，不能影响排版。
    """
    try:
        import json
        from pathlib import Path
        from . import outline
        stat = stat or {}
        items = stat.get('items') or []
        body_start = stat.get('body_start', 0)
        rows, truncated = outline.rows(items, body_start,
                                       resolved_levels=stat.get('resolved_levels') or {})
        data = {
            'rows': rows,
            'truncated': truncated,
            'word_outline': outline.outline_lines(items, body_start),
            'suspicious': bool((stat.get('numbering') or (None, None))[1]),
        }
        Path(work_dir, OUTLINE_FILE).write_text(
            json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception as exc:                      # noqa: BLE001
        logger.warning("DSH outline not saved: %s", exc)


def load_outline(work_dir):
    """读回 save_outline 写的文件；没有或损坏返回 None。"""
    import json
    from pathlib import Path
    try:
        data = json.loads(Path(work_dir, OUTLINE_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('rows'), list) else None
