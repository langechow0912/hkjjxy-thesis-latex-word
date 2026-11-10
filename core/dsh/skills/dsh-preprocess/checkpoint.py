# -*- coding: utf-8 -*-
"""CP1: 文档预处理 —— 只读分析，产出「建议」，不改原稿。

设计约束（很重要，别为了"智能"破坏它们）：
  * 不写回 source.docx。任何"修复"都只是返回给用户看的建议。
  * 模型只看到结构清单，不看到全文正文。
  * 输出必须能解析；解析不了就当没跑过，不阻塞排版。
"""

import json
import re
from typing import Dict, Any

from core.dsh.checkpoints import Checkpoint

# 允许模型提出的动作。超出这个集合的一律丢弃——防止它顺手改正文。
ALLOWED_OPS = {"insert_heading", "adjust_level", "renumber", "rename"}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _strip_fence(text):
    """模型偶尔还是会把 JSON 包在 ``` 里，剥掉。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_suggestions(content):
    """把模型输出解析成规整的建议列表；解析失败返回 None。"""
    raw = _strip_fence(content)
    if not raw:
        return None
    # 模型有时会在 JSON 前后加说明，取最外层花括号
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    issues = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        actions = []
        for act in item.get("actions") or []:
            if not isinstance(act, dict):
                continue
            if act.get("op") not in ALLOWED_OPS:
                continue          # 不认识的动作为了安全直接丢掉
            actions.append(act)
        issues.append({
            "type": str(item.get("type") or "unknown"),
            "severity": (item.get("severity") if item.get("severity") in SEVERITY_ORDER
                         else "medium"),
            "message": str(item.get("message") or "").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
            "target_index": item.get("target_index"),
            "actions": actions,
        })
    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])

    return {
        "issues": issues,
        "notes": [str(n) for n in (data.get("notes") or []) if str(n).strip()],
        "summary": str(data.get("summary") or "").strip(),
        "quality_score": data.get("quality_score"),
    }


class PreprocessCheckpoint(Checkpoint):
    """文档预处理检查点：检查并建议修复标题层级 / 编号 / 题注问题。

    只产出建议，不改写原稿 —— 用户最终决定是否采纳。
    """

    @property
    def name(self) -> str:
        return 'cp1_preprocess'

    def should_run(self, context: Dict[str, Any]) -> bool:
        """只在确实有可疑处时才调模型，省 token。

        规则层（docx2tex.audit / number_headings）已经能确定的问题不麻烦模型；
        模型负责的是「这条无编号短行到底是不是标题」这类判断性灰色地带。
        """
        if not context.get('source'):
            return False
        if self.dsh_config.get('always_run'):
            return True
        return bool(context.get('suspicious'))

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        from core.dsh import outline as outline_mod

        items = context.get('items') or []
        body_start = context.get('body_start', 0)
        # 调试页没有原始 items，只有落盘的结构清单，直接传现成的行
        structure_lines = context.get('structure_lines')
        if structure_lines is None:
            structure_lines = outline_mod.build(
                items, body_start,
                resolved_levels=context.get('resolved_levels') or {})
        outline_lines = context.get('outline_lines')
        if outline_lines is None:
            outline_lines = outline_mod.outline_lines(items, body_start)
        structure = "\n".join(structure_lines)
        outl = "\n".join(outline_lines)

        prompt_file = self._prompt_path()
        template = prompt_file.read_text(encoding='utf-8')
        prompt = (template
                  .replace('{structure}', structure)
                  .replace('{outline}', outl))

        result = await self.client.chat(prompt, temperature=0.2)
        parsed = parse_suggestions(result.get('content', ''))
        cost = int(result.get('usage', {}).get('total_tokens', 0) or 0)

        if parsed is None:
            return {
                'success': False,
                'reason': 'unparseable',
                'actions': [],
                'warnings': ['智能结构检查未能返回可解析的建议，已跳过（不影响排版）。'],
                'cost': cost,
            }

        actions = []
        for issue in parsed['issues']:
            actions.append({
                'type': 'warn' if issue['severity'] == 'high' else 'info',
                'description': issue['message'],
                'data': issue,
            })

        return {
            'success': True,
            'actions': actions,
            'warnings': parsed['notes'],
            'suggestions': parsed['issues'],
            'summary': parsed['summary'],
            'quality_score': parsed['quality_score'],
            'cost': cost,
        }

    def _prompt_path(self):
        """prompt 在 skill 目录里（core/dsh/skills/dsh-preprocess/prompts/）。

        整个 skill 随 core/ 一起进镜像（Dockerfile 只 COPY core/web/schools）。
        """
        from pathlib import Path
        return Path(__file__).resolve().parent / 'prompts' / 'heading_fix.txt'
