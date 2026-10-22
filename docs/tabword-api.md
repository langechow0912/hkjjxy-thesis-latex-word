# Tabword API 接入（2026-10-21）

已逐项阅读官网嵌入 Apifox 的 26 个接口页面及费用、API Key 两个说明页。
入口：https://apihub.tabword.com/#docs 。每页正文快照保存在 `.project-memory/tabword/`，不是仅阅读目录。

## 当前边界

已完成请求适配和本地模拟 HTTP / SSE 测试，**尚未对 Tabword 做真实鉴权、真实任务或收费验证**。用户要求现在不提交。
付费任务默认关闭（`TABWORD_ENABLE_CALLS=false`）：改写、检测、文件处理、异步结果查询及格式接口均不会请求上游。仅开放两个无副作用的只读目录 GET：`presets` 和 `detection_options`，供前端按实际服务商目录显示模型、平台及价格；它们不接收论文内容、不创建任务、不扣费。其他 GET（尤其成功排版结果查询）仍受付费任务开关保护。
文档标记开发中的接口另受 `TABWORD_ENABLE_DEVELOPMENT=false` 保护，官方上线后再启用。
独立设置 `TABWORD_API_KEY`，不复用大以论文 PAT。目前没有提供新平台专用 Key。

## 配置与运行

参考根目录 `.env.example`，通过进程环境变量配置；项目不会自动读取 .env 文件。
`TABWORD_BASE_URL` 默认 `https://api.tabword.com`，来自新官网前端公开配置。
`TABWORD_FORMAT_BASE_URL` 默认 `https://apihub.ycjg.top`，来自格式接口每页 curl 示例。两者只填 origin，不附加 /api；服务方上线时需确认格式域名是否已迁移。
不跟随重定向、不自动重试收费请求，鉴权统一 `Authorization: Bearer <独立 API Key>`。

本次新服务检查运行于 http://127.0.0.1:8766/ ，原 8765 进程保留此前预览任务，避免重启丢失内存任务。
标准启动：`python web/app.py --port 8766`。

## 本地后端协议

- `GET /api/tabword`：本地配置状态及全量接口清单；不会请求服务商、不返回密钥。
- `GET /api/tabword/catalog/presets?mode=...` 与 `GET /api/tabword/catalog/detection_options?language=...`：代理允许的免费目录读取，仅限 `presets` / `detection_options`。
- `POST /api/tabword/{operation}`：multipart，`params` 是 JSON 参数对象，`file` 为可选文件，`submit` 默认 false。
- `submit=false`：构造请求、校验必填/文件/枚举/长度/轮询参数，返回 `submitted=false, charged=false`；不会验证上游可达或文件正文有效性。
- `submit=true`：先检查后端开关、上线状态、独立密钥，再执行请求。同步 JSON 原样保留结构；SSE 实时透传（不能当 JSON 或 EventSource 使用）。
- 未声明参数和操作均拒绝。文件内存上限 20 MB，文件类型按操作区分。上游错误不回显响应正文或密钥；不自动重提任务。
- `core.tabword.call` 用于 JSON，`stream_call` 用于流，`dry_run` 仅本地构造。异步提交与查询分别调用，不自动轮询重试。

## 接口清单

| 本地 operation | 上游方法和路径 | 编码 / 返回 | 必填参数（另见 file） | 文档 |
|---|---|---|---|---|
| `paraphrase` | `POST /api/v2/paraphrase` | json / JSON | text, preset | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259615e0) |
| `paraphrase_stream` | `POST /api/v2/paraphrase/stream` | json / SSE | text, preset | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259616e0) |
| `paraphrase_async` | `POST /api/v2/paraphrase/async` | json / JSON | text, preset | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259617e0) |
| `paraphrase_task` | `GET /api/v2/paraphrase/task/{task_id}` | query / JSON | task_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259618e0) |
| `presets` | `GET /api/v2/presets` | query / JSON | — | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259619e0) |
| `rewrite_options` | `GET /api/v2/rewrite-options` | query / JSON | — | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/514506412e0) 开发中 |
| `preset_detail` | `GET /api/v2/presets/{presets_id}` | query / JSON | presets_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259621e0) |
| `document_paraphrase` | `POST /api/v2/document/paraphrase` | multipart / JSON | , file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259622e0) |
| `estimate_cost` | `POST /api/v2/document/estimate_cost` | multipart / JSON | , file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259623e0) |
| `document_async` | `POST /api/v2/document/paraphrase/async` | multipart / JSON | , file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259624e0) |
| `document_task` | `GET /api/v2/document/task/{task_id}` | query / JSON | task_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259625e0) |
| `detection_options` | `GET /api/v2/detection/options` | query / JSON | — | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259626e0) |
| `detect_text` | `POST /api/v2/detection/check` | json / JSON | text, task_platform | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259627e0) |
| `detect_file` | `POST /api/v2/detection/check-file` | multipart / JSON | task_platform, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/475259628e0) |
| `balance` | `GET /api/business/keys/balance/by-key` | query / JSON | — | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/479383971e0) 开发中 |
| `templates` | `GET /api/templates` | query / JSON | owner_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/503441950e0) 开发中 |
| `template_editor` | `GET /api/public/template-editor/templates/{template_id}/editor-url` | query / JSON | template_id, owner_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/503443286e0) 开发中 |
| `delete_template` | `DELETE /api/public/template-editor/templates/{template_id}` | query / JSON | template_id, owner_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/505626429e0) 开发中 |
| `format_check` | `POST /api/public/format-check/check/report` | multipart / JSON | template_id, owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/509617506e0) 开发中 |
| `format_check_async` | `POST /api/public/format-check/jobs` | multipart / JSON | template_id, owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/509622063e0) 开发中 |
| `format_check_result` | `GET /api/public/format-check/jobs/{job_id}/result` | query / JSON | job_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/509622275e0) 开发中 |
| `create_sample_template` | `POST /api/public/template-editor/analyze-and-create/stream` | multipart / SSE | owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/503443175e0) 开发中 |
| `create_instruction_template` | `POST /api/public/template-editor/instruction-analyze-and-create/stream` | multipart / SSE | owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/505625658e0) 开发中 |
| `format_stream` | `POST /api/public/format/stream` | multipart / SSE | template_id, owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/503444439e0) 开发中 |
| `format_async` | `POST /api/public/format/jobs` | multipart / JSON | template_id, owner_id, file | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/509616912e0) 开发中 |
| `format_result` | `GET /api/public/format/jobs/{job_id}/result` | query / JSON | job_id | [页面](https://s.apifox.cn/a3cc07eb-4d6a-4233-85b6-c52267b7ef29/509617179e0) 开发中 |

## 核对时发现的差异

- Apifox 部分自动 schema 把 SSE 写成 application/json，以正文事件协议为准。支持 progress、complete/done、billing、error；不能把 complete 当成计费结束，billing 在后面。
- 格式接口 schema 漏写 owner_id，但正文及 curl 提供该字段；本地要求 owner_id，确保模板空间一致。
- 同步改写返回 paraphrased_url，异步文档返回 result_url；格式结果为 docxUrl/workbenchUrl，前端分别处理。
- 预设详情 presets_id 使用预设 code，不使用数值 id；模型列表 language 为 ch/en，检测选项为 zh/en。
- 文件检测支持 doc/docx/txt，格式排版支持 docx/pdf，格式检查只支持 docx，说明创建模板支持 pdf/doc/docx。
- 文档改写中文/英文默认值按官方 schema 保留（aigcmove_tp_s_2 / 5.09en）；前端可填代码，不将文档示例当成实时模型列表。
- 收费表为改写按模型单价、AI 检测 0.1 元/次、格式/检查/创建模板 5 元/次；部分响应示例仍写 3 元，不能用示例硬编码结算。
- 异步排版提交检查余额；第一次查询到 SUCCEEDED 时结算。查询也必须遵守“不调用付费服务”的开关。
- 模板删除不可恢复，适配保留 DELETE，仅供明确业务操作；前端未提供删除按钮，本次没有删除任何模板。

## 验证

`python -m unittest discover -s tests -t .`，新增测试用本机 HTTP 假服务覆盖全部 26 个操作，另检查文档路径一致、错误/鉴权/重定向不重试、输入校验、关闭开关零请求、密钥隔离、JSON 和 SSE 后端路由。
`.project-memory/tabword/browser_check.py` 验证实际页面文本 / Word / 格式本地校验、隐藏付费提交按钮、移动端无横向溢出和无 JS 异常。
所有这些均为离线验证，不能等同服务商真实验收。


## 本次真实 Key 验证

2026-10-21 使用用户提供的降重API-湖北.txt，密钥提取到 Git 忽略的 `.secrets/tabword.key`。后端优先环境变量，未设置时读取该文件；无需在页面填写密钥。应用调用开关仍关闭。
实际免费查询：余额成功（账户余额 0 元、Key 额度 0 元），降 AI 预设列表成功（21 个），中文检测平台列表成功（code=0，message=success）。未提交任何改写、检测、格式或模板任务，未进行收费结果查询。付费处理效果尚未验证。
