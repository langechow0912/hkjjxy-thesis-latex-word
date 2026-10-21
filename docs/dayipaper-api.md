# 云端排版通道（大易论文排版开放接口）

把 Word 稿直接交给官方排版引擎，按选定院校的规范一键出成品；适合**还没做本地 profile 的学校**。

| 通道 | 入口 | 适合什么 |
|---|---|---|
| 本地流水线 | `core/build.py`、`python -m core.build` | 已做过 profile 的学校；格式要能改、要出 LaTeX/PDF+Word |
| 云端排版 | `core/dayipaper.py`、`python -m core.dayipaper`、`/api/dayipaper/*` | 没做 profile 的学校；只要一份符合规范的 Word |

两条通道互不依赖：云端挂了不影响本地构建，本地没做 profile 也不影响云端出稿。

---

## 0. 开通道要配的东西

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DAYIPAPER_PAT` | 空 | 开放平台申请的 Personal Access Token。**留空 = 通道关闭**，前端不显示这一块 |
| `DAYIPAPER_BASE` | `https://transport.b1n1.dayipaper.com` | 服务地址，一般不用改 |
| `THESIS_MAX_CLOUD_JOBS` | 3 | 同时进行的云端任务上限（长轮询，不占 CPU，所以和本地编译额度分开算） |
| `THESIS_CLOUD_POLL` | 2 | 轮询间隔秒。**对方要求 ≥ 1 秒**，代码里还会再夹一次 |
| `THESIS_CLOUD_TIMEOUT` | 900 | 轮询总超时秒，超了报错而不是无限挂着 |

PAT 只在请求头里出现（`Authorization: Bearer <PAT>`），不落盘、不进日志、不拼进 URL。

```bash
# 本机临时设置（PowerShell）
$env:DAYIPAPER_PAT = "你的PAT"
# 或写进 .env（已在 .gitignore 里）
```

---

## 1. 调用时序

| 步骤 | 接口 | 产出 | 计费 |
|:--:|---|---|:--:|
| 1 | `GET /searchRule` | `ruleId` 规范 ID | — |
| 2 | `POST /uploadRowDocument` | `fileUri` 临时文件 key | — |
| 3 | `POST /requestDocumentFormat` | `taskId` 任务 ID | — |
| 4 | `GET /traceTask`（轮询） | 状态到 `COMPLETED`，顺带拿到 `pdfShareUrl` 预览 | — |
| 5 | `GET /formatSuggestions` | 识别不确定项的建议 | — |
| 6 | `GET /downloadFormattedDocument` | 成品下载地址（3 分钟有效） | **计费** |

所有接口同一信封：`{code, message, data}`，`code == 0` 才是成功。

---

## 2. 代码在哪

| 文件 | 职责 |
|---|---|
| `core/dayipaper.py` | 六个接口 + `wait()` 轮询 + `download_to()` 现取现下 + `format_document()` 全流程编排 + 命令行 |
| `web/app.py` | `/api/dayipaper/*` 六个路由，复用 `JOBS` 任务表（`kind="cloud"`） |
| `web/static/index.html` | 「云端排版」卡片：搜规范 → 选规范 → 填题目 → 上传 → 轮询 → 预览/导出 |
| `tests/test_dayipaper.py` | 离线端到端测试（本地假服务模拟六个接口，不联网、不计费） |

### 命令行

```bash
# 只查规范（打印 ruleId，不花钱）
python -m core.dayipaper --search 海口经济学院

# 完整出稿：自动取第一条规范，导出 docx
python -m core.dayipaper --file 论文.docx --school 海口经济学院 \
       --title "中文标题" --author "周良" --tutor "李老师" --out out

# 指定规范 ID；先只到预览，确认后再导出（不产生费用）
python -m core.dayipaper --file 论文.docx --rule 6530e4bf69f68d65b4a58e20 \
       --title "中文标题" --author "周良" --no-export
```

### Web 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/dayipaper` | 通道状态（`enabled` / 上限 / 轮询间隔） |
| `GET` | `/api/dayipaper/rules?q=&size=` | 规范搜索 |
| `POST` | `/api/dayipaper/format` | multipart：`file` + `ruleId` + `title` + `authorName`（其余选填）→ `{job}` |
| `GET` | `/api/dayipaper/job/{id}` | `state` / `status` / `statusLabel` / `log` / `suggestions` / `exported` |
| `GET` | `/api/dayipaper/preview/{id}` | 现取状态后 303 跳转到预览 PDF（原地址 3 分钟失效） |
| `GET` | `/api/dayipaper/download/{id}?fmt=1` | 导出成品，**必须带 `confirm=true`**；导过一次后直接给缓存文件 |

---

## 3. 两条红线

### 3.1 导出接口计费

第 6 步按次计费（同一任务最多计一次）。所以：

- `format_document(..., export=False)` 与 CLI 的 `--no-export` 都停在预览，不触达计费接口；
- Web 端 `download` 不带 `confirm=true` 直接 400 拒绝，前端点了还要过一次 `confirm()` 弹窗；
- 同一任务导出成功后文件缓存在任务目录里，再点「重新下载」不会再打一次导出接口。

### 3.2 下载地址 3 分钟失效

`downloadFormattedDocument` 返回的是 OSS 预签名地址，**3 分钟后失效**，而且不能缓存复用。
`download_to()` 每次现取现下；预览同理，`/api/dayipaper/preview/{id}` 每次重新 `traceTask` 再跳转，
把地址存进界面留给用户慢慢点必然打不开。

另外：预签名地址**不能带 `Authorization` 头**（多余鉴权头会被对象存储拒绝），所以下载走的是裸请求。

---

## 4. 错误约定

`core/dayipaper.py` 把所有失败收敛成 `ApiError`（带 `message` / `code` / `http_status`），
Web 层再翻成 HTTP：`400/401/403/413/429` 原样透出，其它归为 `502`。

| 现象 | 实际含义 | 处理 |
|---|---|---|
| `登录状态无效，请先登录`（HTTP 401） | PAT 错、过期、或没带 | 重配 `DAYIPAPER_PAT` |
| `code != 0` 但 HTTP 200 | 业务失败，看 `message` | 按提示改参数 |
| 状态停在 `ERROR` | 排版失败，看 `errorReason` | 常见是源文档打不开 |
| `openDocumentError` / `cantDetectedComponent` | 源稿损坏或没有可识别组件 | 换源稿 |
| 超时报错 | 任务没在 `THESIS_CLOUD_TIMEOUT` 内完成 | 调大超时，或去平台看任务 |

轮询间隔低于 1 秒会被风控拦，`wait()` 里用 `max(interval, 1.0)` 兜住，别在外层绕过。

---

## 5. 实测记录（2026-10-21）

- `transport.b1n1.dayipaper.com` 本机**直连可达**，无需代理；`.NET/PowerShell` 的 HTTPS 栈握手失败是本地 TLS 环境问题，Python `urllib` 正常。
- **鉴权失败返回 HTTP 401 + 正常信封**，不是纯 HTTP 错误 —— 所以实现里先解 body 再判状态，否则会把「PAT 过期」误报成「网络故障」。
- 规范搜索、上传、发起、轮询、建议、导出六个路径与文档一致；`data` 是字符串的接口（上传/发起/导出）直接返回裸值，不要按对象解析。
- 没有真实 PAT，因此未跑通完整出稿链路；真实的 `upload → format → trace → download` 由 `tests/test_dayipaper.py` 用本地假服务覆盖。

---

## 6. 排查清单

- [ ] `GET /api/dayipaper` 里 `enabled` 是 `true`（否则 `DAYIPAPER_PAT` 没生效，改完环境变量要重启服务）
- [ ] 搜到的规范属于目标院校/层次（`targetSchoolName` / `targetSubjectLevel`）
- [ ] 题目、作者名两份必填项都填了（接口硬性要求）
- [ ] 轮询间隔 ≥ 1 秒
- [ ] 导出前先看 `/formatSuggestions` 的提示，很多「排版错位」是识别不出标题导致的
- [ ] 预览/下载点不动时先怀疑 3 分钟过期，界面刷新一次重新取地址

---

## 7. 跑测试

```bash
python -m unittest discover -s tests -t . -v
```

17 项用例覆盖：完整 6 步链路、`export=False` 不计费、轮询下限夹到 1 秒、401 信封翻译、
业务 `code != 0`、`ERROR` 状态带原因、超时、缺 PAT、连不上主机、multipart 文件名转义，
以及 Web 层（不需要 PAT、不联网、不产生费用）。
