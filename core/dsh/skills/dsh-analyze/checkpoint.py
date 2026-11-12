# -*- coding: utf-8 -*-
"""cpx_analyze：识别助手 —— 只产出「待确认的修改」，不改任何文字。

约束（别为了「智能」破坏它们）：
  * 输出里的 idx 必须在发给模型的清单里；role 只能是 4 种；level 只能是 1–4。
  * 不接受任何文字修改；不认识的字段、越界的项整条丢弃。
  * 解析不了就当没跑成，调用方据此全额退积分。
"""
import json
import re
from pathlib import Path
from typing import Any, Dict

from core.dsh.checkpoints import Checkpoint

ROLES = ("heading", "figure_caption", "table_caption", "body")
MAX_CHANGES = 60
MAX_REPLY = 600
MAX_REASON = 80


def _strip_fence(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_reply(content, valid_idx, current=None):
    """把模型输出解析成 {reply, changes}；解析失败返回 None。

    valid_idx  允许修改的 idx 集合（发给模型的清单里出现过的行）
    current    {idx: (role, level)}：已经是这个状态的修改没有意义，丢掉
    """
    raw = _strip_fence(content)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    current = current or {}
    changes, seen = [], set()
    for item in data.get("changes") or []:
        if not isinstance(item, dict):
            continue
        idx, role, level = item.get("idx"), item.get("role"), item.get("level")
        if type(idx) is not int or idx not in valid_idx or idx in seen or role not in ROLES:
            continue
        if role == "heading":
            if type(level) is not int or not 1 <= level <= 4:
                continue
        else:
            level = None
        if current.get(idx) == (role, level):
            continue
        seen.add(idx)
        change = {"idx": idx, "role": role, "reason": str(item.get("reason") or "").strip()[:MAX_REASON]}
        if level:
            change["level"] = level
        changes.append(change)
        if len(changes) >= MAX_CHANGES:
            break
    return {"reply": str(data.get("reply") or "").strip()[:MAX_REPLY], "changes": changes}


class AnalyzeCheckpoint(Checkpoint):
    """识别助手：找识别错的标题 / 图题 / 表题，或按用户的对话指令批量调整。"""

    @property
    def name(self) -> str:
        return 'cpx_analyze'

    def should_run(self, context: Dict[str, Any]) -> bool:
        # 用户主动点的，不看规则层有没有存疑
        return bool(context.get('structure_lines'))

    def prompt_template(self):
        return (Path(__file__).resolve().parent / 'prompts' / 'analyze.txt').read_text(encoding='utf-8')

    def build_prompt(self, context):
        history = "\n".join("%s：%s" % ("用户" if t.get('role') == 'user' else "助手", str(t.get('content', ''))[:500])
                            for t in (context.get('history') or [])[-4:]) or "（无）"
        return (self.prompt_template()
                .replace('{structure}', "\n".join(context['structure_lines']))
                .replace('{edits}', "\n".join(context.get('edit_lines') or []) or "（无）")
                .replace('{history}', history)
                .replace('{message}', (context.get('message') or '请检查整份结构，找出可能识别错的标题、图题、表题。')[:500]))

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        prompt = self.build_prompt(context)
        kwargs = {}
        if context.get('max_tokens'):
            kwargs['max_tokens'] = int(context['max_tokens'])
        # 每次调用都对应一次真实计费，所以不走缓存
        result = await self.client.chat(prompt, temperature=0.2, use_cache=False, **kwargs)
        usage = result.get('usage') or {}
        total = int(usage.get('total_tokens') or 0)
        parsed = parse_reply(result.get('content', ''), set(context.get('valid_idx') or ()), context.get('current'))
        base = {'usage': {'prompt': int(usage.get('prompt_tokens') or 0),
                          'completion': int(usage.get('completion_tokens') or 0), 'total': total},
                'prompt_chars': len(prompt), 'cost': total, 'actions': [], 'warnings': []}
        if parsed is None:
            return {**base, 'success': False, 'reason': 'unparseable'}
        return {**base, 'success': True, **parsed}
