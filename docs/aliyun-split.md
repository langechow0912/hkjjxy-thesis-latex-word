# 阿里云版：主站放阿里云，海口经济学院排版放海外服务器

> 这是 `aliyun-split` 分支的改造，**还没有上线**，线上（AWS）跑的是原来的单机版。

## 为什么这样拆

线上单机版在 AWS 悉尼，到 Tabword（降重 / 检测）要跨境，TLS 握手时好时坏，付款后的检测偶尔卡死。
两类工作对线路的要求不一样：

| 工作 | 失败的后果 | 该放哪 |
|---|---|---|
| 降重 / 检测（Tabword）、云端排版（大易论文）、支付宝 | 付了款才调用，卡了要退款 | **离服务商近**：阿里云主站 |
| 海口经济学院本地排版（LibreOffice） | 排版免费，付款在导出时；卡了、重传都不多收钱 | 可以放远处：海外排版服务器 |

所以：主站放阿里云；海口经济学院的排版仍在原来那台海外服务器（已装 LibreOffice）上做，
主站通过一组内部接口把原稿交过去、取回成品；**连不上就自动改走云端 API**。

## 拓扑

```
用户 ──▶ 阿里云主站（账号 / 积分 / 支付 / Tabword / 云端排版 / 前端）
              │  海口经济学院：原稿 ──▶ 海外排版服务器（LibreOffice，/internal/format-worker/*）
              │                 ◀── docx + pdf（主站拷回本地，之后和本机排版完全一样）
              └─ 排版服务器连不上 ──▶ 云端排版 API（大易论文的海口经济学院规范）
```

选路优先级（`web/app.py` 的 `_school_route`）：**海外排版服务器 → 本机 LibreOffice → 云端 API**。

## 代码里新增了什么

| 文件 | 作用 |
|---|---|
| `core/format_remote.py` | 主站的客户端：上传（幂等、重试）、轮询、下载（SHA-256 校验）、健康检查 + 熔断 |
| `web/format_worker_routes.py` | 排版服务器的内部接口：`/internal/format-worker/{health,jobs,jobs/<id>,jobs/<id>/files/<kind>}` |
| `web/app.py` | `_school_route` 选路、`_run_remote_school` 远程排版、`_remote_fallback` 回退云端、提交入口选路 |

其余（预览、付费下载、24 小时清理、优惠券）一行没改：成品拷回主站任务目录后，和本机排版没有区别。

## 故障时的行为

| 情况 | 行为 |
|---|---|
| 提交时排版服务器连不上 / 健康检查失败 | 这个任务直接走云端 API；报警邮件一次；之后每 20 秒重查，恢复后自动切回 |
| 排版中途连续 3 分钟连不上、对面重启丢了任务、超过 20 分钟 | **同一个任务**改走云端 API（`THESIS_REMOTE_LOST_AFTER` / `THESIS_REMOTE_TIMEOUT`） |
| 对面报告排版本身出错（原稿问题） | 如实报错，**不**改走云端（换云端结果也一样，也避免重复提交） |
| 对面已满（HTTP 429） | 提示"当前排版任务较多，请稍后再提交"，不改走云端（它只是忙） |
| 密钥配错（401/403） | 当作排版服务器不可用，改走云端并报警 |
| 下载成品校验不一致 | 当作没收到，重试 |

回退到云端的任务 `school_key` 仍是 `school:hkjjxy`，限定这所学校的优惠券照常能用；`kind` 变成 `cloud`，导出走云端接口。

## 部署

两台共用同一个镜像和同一份仓库，差异只在各自的 `.env` 和 `deploy/secrets/`。

1. **生成共用密钥**：`openssl rand -hex 32`，两台都写进 `deploy/secrets/format_worker_token`（`chmod 400`，属主 10001）。
2. **排版服务器（海外，装 LibreOffice）**：`THESIS_LOCAL_FORMAT=true`，**不设** `FORMAT_WORKER_URL`。
   有密钥文件即开放内部接口；没有就一律 404。
3. **主站（阿里云）**：
   - `.env`：`THESIS_LOCAL_FORMAT=false`（构建参数同样会读它，镜像不装 LibreOffice，省磁盘和内存）、
     `FORMAT_WORKER_URL=https://<排版服务器域名>`。
   - 内存：不装 LibreOffice 时 2 GB 足够。
   - 其余配置（支付宝、Tabword、SMTP、DeepSeek 等密钥）照搬线上，支付宝回调地址改成新域名。
4. **排版服务器要有自己的域名，并换用专用 Caddy 配置。** 主站搬走后，原来的网站域名会指向阿里云，
   所以排版服务器需要另一个主机名（例如 `format.example.com`，A 记录指向这台）。用
   `Caddyfile.worker`（通过 `docker-compose.worker.yml` 挂上）：只开放 `/internal/format-worker/*`，
   且只允许主站的公网 IP 访问，其余路径一律 404：

   ```bash
   # 排版服务器的 .env：DOMAIN=format.example.com  MAIN_SITE_IPS=<主站公网IP>  THESIS_LOCAL_FORMAT=true
   docker compose -f docker-compose.yml -f docker-compose.worker.yml up -d --build
   ```

   主站的 `FORMAT_WORKER_URL` 填 `https://format.example.com`。AWS 安全组的 443 本来就对外开放，不用改。
   上线前验证：从主站带密钥访问 `/internal/format-worker/health` 应是 200；从别的 IP 访问应是 403；
   访问 `/` 应是 404。**这套 Caddy 配置目前只验证过语法，路由行为要上线前实测。**
5. **验证**：主站上提交一份海口经济学院的稿子；日志里应有"已提交到排版服务器"。
   再把排版服务器停掉，新提交的任务应走云端 API，并收到一封"海外排版服务器连不上"的报警邮件。

## 迁移主站（单机版 → 阿里云）

要搬的东西：`/data` 卷里的 SQLite（账号、订单、积分、优惠券、推广员）、`deploy/secrets/`、`.env`、推广员提现凭证。
停机窗口：先停旧站 → 拷库 → 起新站 → 改 DNS → 验证支付宝回调。上线前需要确认：

- 阿里云区域：**内地**需要 ICP 备案（通常几周）；**香港**不需要。
- 支付宝开放平台里该应用对应的网站 / 回调域名是否需要更新。
- 用户论文会从阿里云发往海外排版服务器：如果主站在内地，这属于数据出境，请自行评估合规要求。

## 测试

```bash
python -m pytest tests/test_format_worker.py -q   # 起一个真实的本机排版服务端，29 个用例
python -m pytest tests -q                         # 全部
```

测试里 LibreOffice 排版用假的 `_run_school` 代替，覆盖：认证、幂等上传、字段白名单、容量限制、校验和、
端到端、连不上 / 中途断线 / 对面重启 / 密钥配错时改走云端、选路、提交入口。
