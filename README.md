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

网页和所有依赖最终都在 AWS EC2 的 Docker 容器里运行；本地电脑只负责把代码提交到 GitHub、必要时用 SSH 连接 EC2 和上传密钥。本地 Docker Desktop 可以关闭，不负责生产网站。仓库已有 `Dockerfile`、`docker-compose.yml` 和 `Caddyfile`：镜像内含 LibreOffice、XeLaTeX、中文字体和 Python 依赖；Caddy 负责 80/443 和自动 HTTPS。

当前已知信息：域名是 `lunwenpaiban.dpdns.org`，EC2 当前公网 IPv4 是 `15.135.187.3`。域名的 NS 已确认由 Cloudflare 管理（`javon.ns.cloudflare.com`、`princess.ns.cloudflare.com`）；目前还没有查到该域名的 A 记录。项目代码已上传到 GitHub 私有仓库 [`langechow0912/hkjjxy-thesis-latex-word`](https://github.com/langechow0912/hkjjxy-thesis-latex-word)，默认分支为 `master`。EC2 可以按下文配置只读 Deploy key 后克隆。

### 1. 代码仓库（GitHub）

项目代码已在私有仓库 `langechow0912/hkjjxy-thesis-latex-word` 的 `master` 分支。EC2 使用只读 Deploy key 克隆；本地电脑不需要再执行首次推送。以后修改代码并发布新版本时，才需要在本地提交并推送：

```powershell
git add -A
git commit -m "Describe the change"
git push
```

### 2. DNS 添加在 Cloudflare（Cloudflare 网页）

不用在 AWS Route 53 添加 DNS。此域名的 NS 已确认指向 Cloudflare；AWS 负责 EC2 和网络，Cloudflare 负责 DNS。Cloudflare → 打开 `lunwenpaiban.dpdns.org` zone → **DNS → Records → Add record**，添加：Type=`A`、Name=`@`、IPv4 address=`15.135.187.3`（若 AWS 中分配了弹性 IP，则填写弹性 IP）、Proxy status=`DNS only` 灰云、TTL=`Auto`。首次让 Caddy 申请证书时先用灰云。目前还没有查到该域名的 A 记录。保存几分钟后，在本地 PowerShell 验证：

```powershell
Resolve-DnsName lunwenpaiban.dpdns.org -Type A
```

### 3. 固定 EC2 地址并开放端口（AWS Console 网页）

AWS Console → **EC2 → Network & Security → Elastic IPs**。若 `15.135.187.3` 已关联为 Elastic IP，直接使用；否则 Allocate Elastic IP，再 Associate 到论文排版 EC2，并把 Cloudflare A 记录同步改成新地址。这样实例停止/启动后域名不会指向旧 IP。

EC2 → **Instances → 选择实例 → Security → Security groups → Inbound rules → Edit inbound rules**。添加/保留 TCP 22，Source=`My IP`；TCP 80 和 TCP 443，Source=`0.0.0.0/0`。只有网站确实启用了 IPv6 才另加 IPv6 来源。不要开放 TCP 8000。查看实例 AMI name 确认系统和 SSH 用户：Ubuntu 通常是 `ubuntu`，Amazon Linux 通常是 `ec2-user`。镜像里的依赖会在首次构建时下载，建议根 EBS 磁盘至少 30 GB。

### 4. 连接 EC2、安装 Docker 和授权 GitHub 只读访问（EC2 终端 + GitHub 网页）

可在 AWS Console 的 **EC2 Instance Connect** 打开终端；也可从本地 PowerShell SSH 连接（按系统替换用户名、密钥路径和 IP）：

```powershell
ssh -i "C:\密钥实际路径\EC2登录密钥.pem" ubuntu@<弹性IP>
```

EC2 终端先运行 `cat /etc/os-release` 确认系统，再执行对应的安装命令。

Ubuntu：

```bash
sudo apt-get update
sudo apt-get install -y docker.io git curl openssl
sudo systemctl enable --now docker
```

Amazon Linux 2023：

```bash
sudo dnf update -y
sudo dnf install -y docker git curl openssl
sudo systemctl enable --now docker
```

两种系统都运行下面命令安装 Compose 插件并确认可用：

```bash
case "$(uname -m)" in
  x86_64) COMPOSE_ARCH=x86_64 ;;
  aarch64) COMPOSE_ARCH=aarch64 ;;
  *) echo "不支持的 CPU 架构：$(uname -m)"; exit 1 ;;
esac
sudo install -d /usr/local/lib/docker/cli-plugins
sudo curl -fSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${COMPOSE_ARCH}" -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
sudo docker --version
sudo docker compose version
```

EC2 从私有 GitHub 拉代码需配置只读 Deploy key。输入 passphrase 时按 Enter 留空，方便以后自动拉取：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/thesis_repo -C "ec2-thesis-readonly"
cat ~/.ssh/thesis_repo.pub
```

只复制 `.pub` 公钥那一整行。在 GitHub 仓库 **Settings → Deploy keys → Add deploy key** 粘贴公钥，Title 填 `EC2 read-only`，不要勾选 **Allow write access**。添加后，在 EC2 首次部署时执行：

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-thesis
    HostName github.com
    User git
    IdentityFile ~/.ssh/thesis_repo
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
mkdir -p ~/apps
cd ~/apps
git clone git@github-thesis:langechow0912/hkjjxy-thesis-latex-word.git
cd hkjjxy-thesis-latex-word
```

之后部署目录是 `~/apps/hkjjxy-thesis-latex-word`。`.secrets/`、`.env`、`deploy/secrets/` 中的运行密钥不要推入 GitHub。

### 5. 配置运行参数和密钥（EC2 终端）

在刚 clone 的项目目录中创建只留在 EC2 的 `.env`：

```bash
cd ~/apps/hkjjxy-thesis-latex-word
cp .env.example .env
nano .env
```

填写或修改下面几项，文件其他默认值保留。不要加引号，也不要在等号两侧加空格：

```dotenv
DOMAIN=lunwenpaiban.dpdns.org
THESIS_ALLOWED_HOSTS=lunwenpaiban.dpdns.org
THESIS_AUTH_USER=admin
THESIS_MAX_JOBS=1
ALIPAY_ENABLED=false
```

保存 nano：按 `Ctrl+O`、Enter，再按 `Ctrl+X`。网页会提示 Basic Auth；用户名是 `admin`，密码从下一条命令生成。`.env` 不提交 GitHub。

```bash
sudo install -d -o 10001 -g 10001 -m 700 deploy/secrets
openssl rand -base64 32 | sudo tee deploy/secrets/thesis_auth_pass
sudo chown 10001:10001 deploy/secrets/thesis_auth_pass
sudo chmod 600 deploy/secrets/thesis_auth_pass
chmod 600 .env
```

命令会显示一次随机密码，同时保存进文件；把它存到自己的密码管理器。Compose 要求这个密码文件存在，缺少时应用不会启动。

其他院校云端排版需要的 Dayipaper PAT 放入 `deploy/secrets/dayipaper_pat`；Tabword 服务需要独立 API Key，放入 `deploy/secrets/tabword_api_key`。海口经济学院本地模板不依赖这两个服务。本地密钥通过 PowerShell `scp` 单独传输，不进 GitHub。以下以 Ubuntu 为例，Amazon Linux 把 `ubuntu` 和 `/home/ubuntu` 分别改为 `ec2-user` 和 `/home/ec2-user`：

```powershell
scp -i "C:\实际路径\EC2登录密钥.pem" "C:\实际路径\应用私钥.pem" ubuntu@<弹性IP>:/home/ubuntu/alipay_app_private_key.pem
scp -i "C:\实际路径\EC2登录密钥.pem" "C:\实际路径\支付宝平台公钥.pem" ubuntu@<弹性IP>:/home/ubuntu/alipay_platform_public_key.pem
```

在 EC2 终端把上传文件安装到 Docker 读取的密钥目录，然后删除家目录临时副本：

```bash
sudo install -o 10001 -g 10001 -m 600 /home/ubuntu/alipay_app_private_key.pem deploy/secrets/alipay_app_private_key.pem
sudo install -o 10001 -g 10001 -m 600 /home/ubuntu/alipay_platform_public_key.pem deploy/secrets/alipay_platform_public_key.pem
rm /home/ubuntu/alipay_app_private_key.pem /home/ubuntu/alipay_platform_public_key.pem
```

支付宝先保持关闭。确认当面付和商户权限已开通、网站 HTTPS 已正常，再把应用私钥和支付宝平台公钥分别作为 PEM 文件放在：

```text
deploy/secrets/alipay_app_private_key.pem
deploy/secrets/alipay_platform_public_key.pem
```

验签使用支付宝平台公钥，不是应用公钥。运行密钥文件权限设为仅容器用户可读：

```bash
sudo chown -R 10001:10001 deploy/secrets
sudo chmod 700 deploy/secrets
sudo find deploy/secrets -type f ! -name README.md ! -name .gitignore -exec chmod 600 {} \;
```

### 6. 构建、启动和检查（EC2 终端 + 浏览器）

```bash
sudo docker compose config -q
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=100 app caddy
curl -fsS https://lunwenpaiban.dpdns.org/api/health
```

首次 build 会下载 Python 依赖、LibreOffice、XeLaTeX 和中文字体，可能需要一段时间。等待 `app` 状态显示 `healthy`、`caddy` 显示运行中，再用浏览器打开 [https://lunwenpaiban.dpdns.org](https://lunwenpaiban.dpdns.org)。浏览器会要求 Basic Auth，输入 `.env` 中的用户名和刚生成的密码。Caddy 会自动申请 HTTPS 证书；若失败，检查 Cloudflare A 记录是否指向弹性 IP，以及安全组是否允许 TCP 80/443。

PDF 预览仍免费。Word 下载在支付宝配置完整并收到匹配的支付通知前会被拒绝。确认当面付权限、商户号及密钥等条件后，才在 EC2 的 `.env` 设置：

```dotenv
ALIPAY_ENABLED=true
ALIPAY_APP_ID=2021007108658866
ALIPAY_SELLER_ID=<支付宝商户PID>
ALIPAY_NOTIFY_URL=https://lunwenpaiban.dpdns.org/api/alipay/notify
ALIPAY_MODE=production
THESIS_FORMAT_PRICE_CENTS=2999
```

保存后在 EC2 项目目录执行 `sudo docker compose up -d` 使配置生效。若当面付尚未开通或商户 ID、密钥不匹配，支付宝无法创建正式订单；代码不会绕过付款。

### 7. 后续更新（本地电脑，再到 EC2）

本地改好代码后提交推送：

```powershell
git add -A
git commit -m "Describe the change"
git push
```

然后在 EC2 终端更新并重建镜像：

```bash
cd ~/apps/<仓库名>
git pull --ff-only
sudo docker compose up -d --build
sudo docker compose ps
```

订单数据库和 Caddy 证书放在 Docker 持久卷里；排版任务状态保存在应用内存，容器重启会清掉正在进行的任务。更新前等当前任务完成。应用端口 8000 不应暴露到公网。

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
