# 论文排版工坊

统一流程：搜索院校规范 → 上传 Word、填写题目与作者 → 排版 → PDF 预览 → 下载 Word。

海口经济学院使用现有学校模板及排版系统，不依赖大以论文 PAT。其他院校由后端调用大以论文 API。前端没有本地/云端选项，也不显示 LaTeX。页面采用暖白、墨色、绿色强调的简洁工作台。

## 运行与验证

```powershell
python -m pip install -r requirements.txt
python web/app.py --port 8765
python -m unittest discover -s tests -t . -q
```

打开 http://127.0.0.1:8765/ ，不要直接双击 HTML。学校排版需要 LibreOffice；工具路径按 schools/hkjjxy/profile.yaml 与 core/tools.py 的配置解析。

## EC2 + Docker Compose + Caddy 部署

仓库已包含 `Dockerfile`、`docker-compose.yml` 和 `Caddyfile`。镜像内带
LibreOffice、XeLaTeX、中文字体和 Python 依赖；应用容器只加入内部网络，Caddy
负责 80/443 和自动 HTTPS。生产 Compose 强制 Basic Auth，应用容器以 UID/GID
10001 的非 root 用户运行，并把支付宝订单数据库放在持久卷中。

### 1. DNS、EC2 和 GitHub

给 EC2 绑定弹性 IP，在 DNS 为域名添加 A 记录。安全组对公网网站开放 TCP 80/443；
SSH 22 只允许你的固定 IP 或 EC2 Instance Connect 使用的来源。EC2 上安装 Docker
Engine 和 Compose 插件。项目代码和依赖已放在私有仓库
`langechow0912/hkjjxy-thesis-latex-word`；为让 EC2 拉取私有仓库，可在 EC2 生成 SSH
密钥，并将公钥添加到仓库 Settings → Deploy keys（保持只读，不启用写权限）：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/thesis_repo -C "ec2-thesis-readonly"
cat ~/.ssh/thesis_repo.pub
```

添加公钥后，在 EC2 首次部署时执行：

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-thesis
    HostName github.com
    User git
    IdentityFile ~/.ssh/thesis_repo
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git clone git@github-thesis:langechow0912/hkjjxy-thesis-latex-word.git
cd hkjjxy-thesis-latex-word
```

不要把 `.secrets/`、`.env`、`deploy/secrets/` 中的运行密钥推入仓库。

### 2. 服务器上的配置

```bash
cp .env.example .env
mkdir -p deploy/secrets
openssl rand -base64 32 > deploy/secrets/thesis_auth_pass
chmod 700 deploy/secrets && chmod 600 deploy/secrets/thesis_auth_pass
chmod 600 .env
```

编辑 `.env`，至少填写 `DOMAIN`、`THESIS_AUTH_USER`；多个域名或 `www` 别名可在
`THESIS_ALLOWED_HOSTS` 中用逗号列出，并保留
`THESIS_ENV=production`、`THESIS_REQUIRE_AUTH=true`。`DOMAIN` 必须只写域名，不能
带协议或路径。把可用的 Dayipaper PAT、独立 Tabword key、支付宝应用私钥和
**支付宝平台公钥**分别放入 `deploy/secrets/` 的同名文件；不要把应用公钥当作
平台公钥。支付宝的 `ALIPAY_NOTIFY_URL` 应填写
`https://你的域名/api/alipay/notify`，并确认当面付和商户权限已开通。填好运行密钥后
执行 `sudo chown -R 10001:10001 deploy/secrets`，并确保目录为 700、密钥文件为
600（可用 `find deploy/secrets -type f ! -name README.md ! -name .gitignore -exec chmod 600 {} \\;`）。当前支付宝
配置仍默认关闭；只有商户资料、验签公钥、私钥文件和回调地址都准备好后再设
`ALIPAY_ENABLED=true`。

### 3. 启动和检查

```bash
docker compose config
docker compose up -d --build
docker compose ps
curl -fsS https://你的域名/api/health
docker compose logs --tail=100 app caddy
```

第一次启动时 Caddy 会为域名申请证书；DNS 和 80/443 入站规则必须已经生效。TeX
Live 和 LibreOffice 会使镜像较大；低内存 EC2 可把 `THESIS_MAX_JOBS=1`。
应用的 `/docs`、`/redoc` 和 `/openapi.json` 在生产模式关闭。PDF 预览仍免费，
Word 导出必须完成支付宝订单；未完成商户配置时会安全地拒绝下载，不会绕过付款。

### 4. 更新和备份

```bash
git pull --ff-only
docker compose up -d --build
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"
```

订单数据库位于 `thesis_data` 卷。排版任务本身仍是进程内存状态，容器重启会丢失
未完成任务；正式使用前应避免在任务运行期间更新，并按 EC2 磁盘快照策略备份
`thesis_data`。不要直接把应用容器端口暴露到公网。

启动时读取 `.secrets/dayipaper.pat`，支持原始令牌或 `PAT=令牌` 单行文件；`DAYIPAPER_PAT` 环境变量优先（环境变量应直接填写令牌值）。凭证仅在后端使用，已从 Git/Docker 上下文排除。修改凭证后重启服务。

## 统一接口与扩展

- `GET /api/format/rules?q=院校名称&size=20`：先匹配已安装学校规范，否则查询服务方。
- `POST /api/format`：multipart file、ruleId、title、authorName 与可选元信息。
- `GET /api/format/jobs/{id}`：进度、建议、预览及计费标记，不返回内部日志。
- `GET /api/format/jobs/{id}/preview`：学校 PDF 或新获取的服务方预览地址。
- `GET /api/format/jobs/{id}/download?fmt=1`：下载 Word 前需完成支付宝扫码支付；学校与云端排版均适用。海口经济学院 PDF 预览和下载免费（`fmt=2`）。

规范 ID 使用 `school:hkjjxy`、`dayipaper:<id>` 命名空间。以后可在后端增加服务适配，继续复用统一任务、预览和下载交互。当前尚不是通用插件注册框架；其他功能 API 应按其实际输入、输出与计费规则增加独立适配。

## 实测状态（2026-10-21）

30 项离线测试通过。真实海口经济学院链路已生成并下载 Word 和 7 页 PDF。真实大以论文已通过海南大学规范查询、上传、排版、状态、建议及 8 页 PDF 下载；用户已选择暂不收费、保留预览测试结果，未执行收费 Word 导出。

PAT 本身有效。此前 401 是凭证读取代码把 `PAT=` 标签一并发送导致，现已修复并加入请求头回归测试。

浏览器已验证真实学校提交、刷新恢复、下载、其他院校搜索、收费取消、无效文件及桌面/手机布局。测试是小型样稿，不代表全部复杂论文及学校最新规则已验收。封面未填写的学号、学院、日期需下载后补全；服务方样稿封面也有待填写字段。

任务保存在服务进程内，重启后不可恢复；浏览器刷新可恢复当前进程任务与已填元信息，原文件需重新选择后才能发起新任务。


## Tabword 在线降 AI 与格式服务

已按新文档逐页接入全部 26 个接口，详见 [Tabword 接入说明](docs/tabword-api.md)。前端提供段落 / Word 降 AI、文本 / 文件 AI 率检测及待上线的格式排版 / 检查入口。
当前按用户要求只做本地请求校验，未提交真实任务、未产生费用。配置见 `.env.example`，需独立 Tabword API Key；不会沿用大以论文 PAT。环境变量必须显式设置，.env 不会自动加载。
新版本本次运行在 http://127.0.0.1:8766/ ，原 8765 保留此前预览任务。26 个接口完成离线模拟验证，上线域名、真实鉴权和收费效果仍待正式接通时验证。

## 支付宝扫码支付

排版完成后可通过支付宝当面付二维码购买 Word 导出，当前标价为 29.99 元，学校模板与云端排版均适用。PDF 预览免费。新用户减 10 元暂不启用，等接入登录后再按账户判断资格。

正式配置项见 `.env.example`。应用只读取进程环境变量，不会自动加载 `.env` 文件。需先在支付宝开放平台开通当面付，并准备应用私钥、支付宝公钥、商户 ID 和可从公网访问的 HTTPS 异步通知地址。通知验签使用支付宝公钥，不是应用公钥。`ALIPAY_ENABLED` 默认为 `false`；配置完整后才允许创建订单。离线测试不创建支付宝订单；正式收款状态仍需在商户开放平台确认并联调。
