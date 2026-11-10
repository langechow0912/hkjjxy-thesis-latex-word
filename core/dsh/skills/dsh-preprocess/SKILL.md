---
name: dsh-preprocess
description: 论文文档智能预处理 - 检查并修复标题层级、章节结构、图表编号等问题
version: 1.0.0
author: hkjjxy-thesis
checkpoint: cp1_preprocess
---

# DSH 预处理 Skill

> 本 skill 放在 `core/dsh/skills/`，随 `core/` 一起进镜像；prompt 就是本目录的
> `prompts/heading_fix.txt`，只有这一份。

在排版流程中检查并修复文档结构问题。所有修复以**建议**形式反馈给用户，
不直接改写原稿——用户最终决定是否采纳。

## 检查项

| 检查项 | 说明 | 严重度 |
|--------|------|--------|
| `heading_orphan` | 二级/三级标题缺少上级标题 | 高 |
| `heading_numbering` | 标题编号不连续或错误 | 高 |
| `heading_skipped` | 跳级（1 级直接到 3 级） | 中 |
| `heading_unnumbered` | 标题缺少编号 | 中 |
| `empty_chapter` | 章节标题下无正文 | 中 |
| `figure_caption` | 图片缺少题注 | 低 |
| `table_caption` | 表格缺少题注 | 低 |
| `ref_format` | 参考文献格式不一致 | 低 |

## 输出格式

```json
{
  "issues": [
    {
      "type": "heading_orphan",
      "severity": "high",
      "message": "标题「2.1 研究背景」缺少上级标题「2 相关理论」",
      "suggestion": "在「2.1 研究背景」之前插入二级标题「2 相关理论」",
      "location": {"block_index": 12, "level": 2}
    }
  ],
  "summary": "共发现 3 个结构问题，其中 1 个高严重度",
  "quality_score": 78
}
```

## 严重度与处理

- `high` → 排版完成后在结果页**重点提示**，建议用户优先处理
- `medium` → 普通提示
- `low` → 归入"其他建议"

任何严重度都不阻断排版流程。

## 上下文

`execute()` 收到的 context：

```python
{
    'source': '/path/to/source.docx',   # 原始 DOCX
    'tex': '/path/to/main.tex',         # docx2tex 产出的 tex（可选）
    'struct': {...},                    # texparse 解析出的结构（可选）
    'work_dir': '/path/to/work',
    'meta': {'title': '...', 'author': '...'},
    'school_id': 'hkjjxy'
}
```

返回：

```python
{
    'success': True,
    'actions': [
        {'type': 'fix', 'description': '建议在「2.1 研究背景」之前插入二级标题「2 相关理论」',
         'data': {...}},
        {'type': 'warn', 'description': '3 个标题缺少上级标题'}
    ],
    'warnings': ['...'],
    'cost': 1234
}
```
