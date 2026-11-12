---
name: dsh-analyze
description: AI 助手 - 在识别结果预览里找出可能识别错的标题 / 图题 / 表题，并按用户的对话指令批量调整
version: 1.0.0
author: hkjjxy-thesis
checkpoint: cpx_analyze
---

# DSH 识别助手 Skill

用户在排版前的「识别结果预览」里点 AI 扫描或和 AI 对话时运行。

- 输入是 `core/dsh/outline.analysis_lines()` 压出的结构清单：标题、图题、表题、表注和标题疑似行的文字，
  正文只有字数，表格单元格不给。每行带 `idx`。
- 输出只有 `{reply, changes:[{idx, role, level?, reason}]}`。`role` 只能是
  `heading / figure_caption / table_caption / body`，`idx` 必须在清单里；
  **不允许模型改文字**，不认识的整条丢弃。
- 它只给「待确认的修改」，由用户在界面里采纳或忽略，不直接改文档。
- 计费按 `usage` 的 token 数，由 `core/points.py` 的预留 / 结算处理；本 skill 只负责返回 `usage`。
