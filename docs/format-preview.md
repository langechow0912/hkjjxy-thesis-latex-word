# 识别结果预览 + AI 助手

排版前让用户先看规则识别出的标题 / 图题 / 表题，直接改；认不准时可以让 AI 找可疑处、用对话微调；确认后排版用的就是改好的结果。

## 用户流程
上传 `.docx` → 「检查识别结果」→ 预览面板（规则识别，免费）→ 手动改 / 「找同格式行」/ AI 助手 → 「确认并排版」。
不点「检查识别结果」就是原来的流程，一点没变。预览面板**默认只显示「待确认」的行**（缺上级、没有题注、像题注但没被识别、AI 的建议等），没问题时直接提示可以排版；点「全部」才看完整列表。用户自己改过的行即使不再有问题也会留在待确认视图里，免得改完就消失。`.doc` 暂不支持预览（要走 LibreOffice，慢），仍可直接排版。

AI 也没识别出来时：① 手动改永远可用；② 「找同格式行」不用 AI 批量设置；③ 和 AI 对话微调；④ AI 没发现问题时界面会写「不代表一定没问题」。

## 数据与接口
- 逐条结果 `docx2tex.analyze(path, overrides)`：`{idx, kind, level, text, num, src, flags…}`。`idx` 是 `read_items()` 的列表下标——**不用 `pno`**（表格会和下一段共用 `pno`）。同一份字节两次读取下标一致，所以排版必须用服务端保存的那份字节（`analysisId`），不能让浏览器重新上传。
- 用户的手动修改 `overrides = [{idx, role?, level?, text?}]`，`role` ∈ `heading / figure_caption / table_caption / body`。`docx2tex.normalize_overrides` 做白名单校验，非法项整条丢弃并回报。
- 覆盖项进入识别的位置：`headings.plan(force=…)`（强制标题 / 强制非标题，走同一套压缩层级逻辑，不受参考文献/致谢区拦截）、`scan()` 里统一的 `cap()` 包装（强制图题 / 表题 / 取消题注，`tabmap` 预扫描与主循环共用，所以「如表 2」的引用编号不会错位）、`scan()` 开头改文字。编号、目录、`\ref` 都在后面重排，编辑自动传导。不传 `overrides` 时与以前逐字节一致。
- 接口（`web/format_ai_routes.py`，都要登录，归属不对一律 404）：
  - `POST /api/format/analyze` 上传并识别 → `{analysis_id, rows, summary, notes, ai}`
  - `GET /api/format/analyze/{id}` 刷新后恢复；`POST …/preview {overrides}` 按用户的修改重新识别，看到「实际会变成什么」
  - `POST …/similar {idx}` 找同格式行（同样式 / 加粗 / 编号形式 / 字号对齐）
  - `POST …/ai`、`POST …/chat {message}` AI 助手
  - `POST /api/format` 新增可选字段 `analysisId`、`overrides`（`file` 变可选）
- 分析记录在 `ANALYSES`（**不进 `JOBS`**，免得被 `KEEP_JOBS` 回收、被 `_running_count` 计数、被 `/api/job/{id}` 暴露）；TTL 30 分钟，由 `_sweep` 清理；每人最多 3 份；重启即丢（前端按 404 提示重新检查）。

## AI 助手（`core/dsh/skills/dsh-analyze/`，检查点 `cpx_analyze`）
- 只产出「待确认的修改」`{idx, role, level?, reason}`，用户采纳或忽略；**不能改文字**，越界 idx / 未知 role / 重复项整条丢弃。
- 隐私：发给模型的只有标题、图题、表题、表注和标题疑似行的文字，**正文只给字数**，表格内容不给（`outline.analysis_lines`）。
- **按实际 token 计费**（`core/points.py`）：先按保守上限**预留**（余额不足 402，不调模型）→ 调模型 → 按 `usage.total_tokens` **结算**（`max(最低额, 用量×单价)`，多退少补）；调用失败 / 超时 / 解析失败**全额退还**；崩溃遗留的预留超过 10 分钟会在下次读余额时自动退回。后台「积分设置」可调：每 1000 token 的积分（默认 0.2）、每次最低（0.5）、单次输出上限（3000）。**这三个默认值只是占位，上线前按 DeepSeek 真实账单校准。**
- 该 skill 把 `reasoning_effort` 设为 `off`、不走缓存（每次计费对应一次真实调用）；每份文档最多 12 次调用，另受 `CostController` 的全局日配额和每人每日次数限制。
- 界面逐条显示本次 token、扣了多少积分、本文档累计。
