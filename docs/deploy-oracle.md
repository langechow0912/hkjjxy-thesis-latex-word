# 部署到 Oracle Cloud Always Free ARM 实例

这套流水线要跑 XeLaTeX + LibreOffice，镜像 3~4 GB、单次构建 1~2 分钟 CPU，
Serverless（Cloudflare Workers 等）跑不了，所以选一台真正的虚拟机。
Oracle Cloud 的 **Ampere A1（4 核 ARM / 24 GB 内存 / 200 GB 磁盘）永久免费**，是目前唯一够用的免费方案。

---

## 零、准备清单

- [ ] Oracle Cloud 账号（注册需信用卡验证，但 Always Free 不扣费）
- [ ] 一个域名，能改 DNS（后面要加 A 记录）
- [ ] SSH 客户端（Windows 用自带的 `ssh` 或 PowerShell 即可）

---

## 一、创建实例

控制台 → 计算 → 实例 → 创建实例：

| 项 | 选什么 |
|---|---|
| 名称 | 随意，如 `thesis` |
| 映像 | **Canonical Ubuntu 24.04**（默认） |
| 配置 | **Ampere ARM**（点"编辑"后切到 Ampere A1 Compute） |
| 规格 | VM.Standard.A1.Flex，**4 核 OCPU / 24 GB 内存**（免费额度上限） |
| 引导卷 | 200 GB（免费额度上限，镜像要占 4 GB，够用） |
| SSH 密钥 | 下载私钥，记下路径 |

> 如果提示"资源不足（Out of host capacity）"，换一个可用性域，或隔几小时重试——
> ARM 免费实例经常抢不到，这是已知情况。

创建完成后记下 **公网 IP**。

---

## 二、放行 80 / 443 端口

Oracle 有两层防火墙，**两层都要开**，否则 Caddy 申请不到证书。

**1）云侧安全列表（VCN）**
虚拟云网络 → 你的 VCN → 子网 → 安全列表 → 添加入站规则：

| 源 | 协议 | 目标端口 |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

**2）实例内部 iptables**（Ubuntu 镜像默认拦掉了 80/443）

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## 三、DNS 解析

在你的域名服务商加一条 A 记录：

```
类型 A    名称 thesis（或 @）    值 <实例公网 IP>    TTL 600
```

等 1~5 分钟生效，用 `nslookup thesis.你的域名.com` 确认解析到该 IP。
**必须先解析再启动 Caddy**，否则 Let's Encrypt 验证失败会反复重试。

---

## 四、安装 Docker

```bash
ssh -i 你的私钥 ubuntu@<公网IP>

curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# 重新登录使组生效
exit
ssh -i 你的私钥 ubuntu@<公网IP>
docker version   # 应能同时看到 Client 与 Server
```

---

## 五、放上代码

```bash
sudo mkdir -p /opt/thesis && sudo chown $USER /opt/thesis
cd /opt/thesis

# 方式 A：从私有仓库拉（推荐，便于后续更新）
git clone https://github.com/langechow0912/hkjjxy-thesis-latex-word.git .

# 方式 B：从本机 rsync（Windows PowerShell 里执行）
# scp -i 私钥 -r C:\...\hkjjxy-thesis-format ubuntu@<IP>:/opt/thesis
```

配置域名：

```bash
cp .env.example .env
nano .env          # 把 DOMAIN= 改成你的域名，如 thesis.你的域名.com
```

---

## 六、构建并启动

首次构建要装 TeX Live + LibreOffice + 中文字体，**约 15~30 分钟**，建议用 `screen` 防止断线：

```bash
screen -S build     # 断线后可 screen -r build 回来
docker compose -f docker-compose.server.yml up -d --build
```

启动后：

```bash
docker compose -f docker-compose.server.yml ps    # 两个容器都应是 Up
docker compose -f docker-compose.server.yml logs -f caddy
```

看到 Caddy 日志里出现 `certificate obtained successfully` 就说明 HTTPS 好了。
浏览器打开 `https://你的域名` 即可。

---

## 七、首次自检（重要，别跳过）

**1）确认中文字体已被识别**

```bash
docker compose -f docker-compose.server.yml exec thesis fc-list :lang=zh family | sort -u
```

应能看到 `Noto Serif CJK SC`、`Noto Sans CJK SC`、`AR PL UKai CN` 等。
**如果列表是空的，PDF 里的中文会全部变成方块**，先排查这一步再往下走。

**2）在容器里跑一篇真实论文**

```bash
docker compose -f docker-compose.server.yml exec thesis \
  python -m core.build --school hkjjxy --sample "企业筹资风险研究"
```

看到 `XeLaTeX 编译 3 遍完成` 且 `编译提示：0 条` 即正常。
产物在 `samples/hkjjxy/企业筹资风险研究/` 下（`main.pdf` 与可编辑 docx）。

**3）打开 PDF 抽查**

把 `main.pdf` 拷回本机看一眼，重点确认：中文不是方块、宋体正文、黑体标题、
页眉已去掉（页码在页脚居中）、目录页码正确。

---

## 八、强烈建议：加访问密码

这个服务会执行上传文件的编译，**裸奔在公网有风险**。生成一个密码哈希：

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext '你想设的密码'
```

把输出的哈希填进 `deploy/Caddyfile`，取消 `basicauth { ... }` 三行的注释，然后：

```bash
docker compose -f docker-compose.server.yml restart caddy
```

---

## 九、日常运维

```bash
# 看实时日志
docker compose -f docker-compose.server.yml logs -f

# 更新代码后重新构建
cd /opt/thesis && git pull
docker compose -f docker-compose.server.yml up -d --build

# 备份论文数据（samples 目录挂载在宿主机，直接打包即可）
tar czf ~/samples-$(date +%F).tar.gz samples

# 看资源占用
docker stats
```

**资源参考**：24 GB 内存对这套流水线非常宽裕——单次构建峰值约 1~2 GB。
4 核可以同时跑 3~4 个构建任务，个人/小班使用完全够。

---

## 常见问题

**Q：字体列表空的 / PDF 中文是方块**
`fc-cache -f` 后再 `fc-list :lang=zh`。若仍为空，说明 `fonts-noto-cjk` 没装上，
检查构建日志里 apt 那一步是否成功。

**Q：LibreOffice 转换失败、任务卡住**
容器里 LibreOffice 需要可写的 `HOME`，Dockerfile 里已设 `HOME=/tmp`。
若仍失败，多半是内存不足或 `/tmp` 被写满：`df -h /tmp`、`docker stats` 看一下。

**Q：Caddy 一直拿不到证书**
按顺序查：① DNS 是否真解析到这台机（`nslookup`）；② 80 端口是否放行
（`curl http://你的域名` 从外网试，应能连上而非超时）；③ 安全列表与 iptables 是否都开了。

**Q：构建任务很慢**
一篇 1~2 分钟是正常的（XeLaTeX 3 遍 + LibreOffice 2 次转换）。
第一次会慢一些，因为要生成字体缓存。

**Q：想换端口或改并发**
改 `docker-compose.server.yml` 里 `PORT` 与 Caddy 的 `reverse_proxy` 目标端口即可，
两边保持一致。
