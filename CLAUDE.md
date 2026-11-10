# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

论文排版 + 降重/降 AI/AIGC 检测的收费网站（FastAPI + 原生 JS 单页，Docker/Caddy 部署）。文档和界面都是中文。

## 常用命令

```bash
python -m pip install -r requirements-local.txt     # 跑测试需要这个，不是 requirements.txt（缺 python-docx、PyMuPDF、PyYAML、qrcode）
python web/app.py --port 8765                       # 本地启动，http://127.0.0.1:8765/
python -m pytest tests -q                           # 全部测试（离线，不联网、不产生费用）
python -m pytest tests/test_accounts.py::AccountTests::test_login_logout_and_wrong_password   # 单个用例
python -m unittest discover -s tests -t . -q        # 不用 pytest 时的等价写法（pytest 本地已装，但不在 requirements 里）
docker compose up -d --build                        # 部署/更新；改了 THESIS_LOCAL_FORMAT 必须重新构建
```

没有配置 lint 或格式化工具。环境变量不会自动加载 `.env`，本地要显式 export。`.secrets/start-local.sh` 会设置**正式环境**的支付宝、大以论文、Tabword 密钥，用它启动的预览里真付款会真扣钱、真调用付费接口。

`tests/test_paid_download.py` 里支付宝查询频率限制相关的用例偶发失败（约 1/6，原因没查清），单独重跑能过。

## 架构

**两条排版链路，按 `ruleId` 分流**（`web/app.py` 的 `api_dp_format`）：
- `school:hkjjxy` → 本地链路 `core/schoolformat.run`：`docx2tex`（Word→tex 字符串，只当中间表示）→ `texparse` → `toc.probe` → `docxbuild`（生成 Word）→ LibreOffice 转 PDF → `preview_watermark`。需要 `soffice`，站点不编译 LaTeX。
- `dayipaper:<id>` → 云端链路 `core/dayipaper.py`，后台线程轮询服务方。
- `THESIS_LOCAL_FORMAT=false` 时海口经济学院也走云端，镜像不装 LibreOffice（构建参数和运行时变量要一起设）。
- 本地链路目前写死 `hkjjxy`（`web/app.py` 的 `LOCAL_SCHOOL`、`_run_school`），新增学校不只是加 `schools/<id>/`，见 `docs/new-school-guide.md`。

**任务在内存里**（`web/app.py` 的 `JOBS`）。完成的任务把 `job.json` 写进自己的临时目录，启动时 `_restore_jobs` 扫描恢复；运行中的任务重启就丢。

**两种付费商品共用一个 SQLite**（`ALIPAY_ORDER_DB`，默认 `.secrets/alipay-orders.sqlite3`）：
- `payment_orders`（`core/alipay.py`）的 `job_id` 列有两种值：排版任务 `j<32 位 hex>`，降重/检测报价 `TW-<32 位 hex>`。
- 降重/检测流程（`core/service_billing.py` + `web/tabword_routes.py`）：`quote` → `create_payment` → 支付 → `authorize` → `record`。付款绑定操作、参数、文件摘要，`authorize` 用一次原子更新把 `ready` 改成 `processing`，保证一笔付款只提交一次。**提交过的订单任何情况下不自动重试**，失败的留给人工核查。
- 付款确认有两条路：支付宝异步通知，和 `alipay.sync_order` 主动 `trade.query`（有频率限制）。本地没有公网通知地址，只靠后者。
- 账号、会话、上传记录（`core/accounts.py`）和推广员/优惠券（`core/affiliates.py`）也在这个库里。

**付款后结果只保留 24 小时**（`alipay.RESULT_TTL`）：`web/app.py` 的 `_live_job` 每次访问先检查并返回 410，后台线程 `_sweep` 每 10 分钟清理，`service_billing.get_quote` 读取时也会惰性过期。订单和金额记录不删。

**登录**：上传、报价、创建支付订单都要登录（`Depends(current_user)`）。管理后台是另一套认证（Basic 或一次性令牌，只显示一次，库里存哈希）。

**前端**是 `web/static/` 下无构建的 `index.html` + `app.js`，每个静态文件在 `web/app.py` 里有单独的路由，没有整体挂载，新增静态文件要加路由。

**标题识别与编号**：`core/headings.py` 的 `plan()` 通读全文给标题定级。认「第X章 / 一、/（一）/ 1. / 1.1 /（1）/ ①」等编号、Word 标题样式和加粗短行；`1 / 1.1 / 1.1.1` 层级固定，`（一）/ 1. /（1）/ ①` 按嵌套顺序推断。原编号一律剥掉再由模板重新编号；缺上级时压缩层级（不编造标题、不再用星号保留原编号）；超过第 4 级的降为加粗正文。每次自动调整都写进 `stat['notes']` 展示给用户。`docx2tex.number_headings` 只负责判断原稿编号是否完整（完整就沿用，否则按顺序自动编号）。图题在图上方、下方、同段都认（`docx2tex.scan` + `core/figures.py`），表格单元格里的图会移到表后。

**Tabword 服务**（`core/tabword.py` + `web/tabword_routes.py`）：26 个接口涵盖降 AI/检测/格式排版，详见 `docs/tabword-api.md`。付费任务默认关闭（`TABWORD_ENABLE_CALLS=false`），只开放免费目录 GET（`presets`、`detection_options`）。需独立 `TABWORD_API_KEY`，不复用大以论文 PAT。

**DSH（DeepSeek Harness，`core/dsh/`）**：排版流程里的智能检查点框架，只产出建议，不改原稿，失败不影响排版。每个功能是 `core/dsh/skills/<name>/`（`SKILL.md` + `checkpoint.py` + `prompts/`），`DSHManager` 自动扫描；新增功能只加目录。必须放在 `core/` 下，镜像不 COPY `.claude/`。模型只收到 `outline.py` 压出的结构清单（正文只给字数）。登录用户调试页 `/dsh`（`web/dsh_routes.py`）可看目录、手动运行、对话，会真调 DeepSeek，受 `dsh.chat.per_user_daily` 限制。详见 `core/dsh/README.md`。

**优惠券与推广员**（`core/affiliates.py`）：管理后台 `/admin/affiliates` 可创建全平台或按 `school:<id>` / `dayipaper:<id>` 限定的优惠券，支付下单时服务端校验并冻结优惠金额。推广员入口 `/affiliate`，新增时显示一次性访问令牌。推广员可申请提现，管理员审核后手动转账并上传凭证（保存在 `/data` 卷）。

## 注意

- 测试：`tests/__init__.py` 把账号库换成临时库，并把 `current_user` 覆盖成固定用户；`tests/test_accounts.py` 会临时去掉这个覆盖来测真实登录。其他测试用 `patch.dict(os.environ, {'ALIPAY_ORDER_DB': ...})` 隔离订单库。
- 仓库要求 LF 换行（`.gitattributes`）。在 Windows 上用脚本写文件容易写成 CRLF，提交前检查。
- `README.md` 有一部分过时：说前端没有本地排版分支、Docker 只装 `requirements.txt`，而代码里本地链路还在，`Dockerfile` 装的是 `requirements-local.txt`。部署步骤（EC2、Cloudflare、Caddy）部分仍然有效。
- `docs/lessons-learned.md` 记录了排版流水线的坑，核心一条：编译或转换退出码为 0 不代表产物正确（缺图也能出 PDF），要单独核对产物。
- **添加新学校**：不是只加 `schools/<id>/`，需同步改 `web/app.py` 的 `LOCAL_SCHOOL` 和 `_run_school`，见 `docs/new-school-guide.md` 完整流程。
- **文档索引**：`docs/dayipaper-api.md`（云端排版）、`docs/tabword-api.md`（降 AI/检测）、`docs/service-billing.md`（收费流程）、`docs/new-school-guide.md`（本地学校扩展）、`docs/lessons-learned.md`（排版踩坑）。
