# 本地部署 + Cloudflare Tunnel 公网访问

本机跑排版服务，用 Cloudflare Tunnel 把 `127.0.0.1:8000` 映射到你自己的域名上，免费、自带 HTTPS、不需要公网 IP。

甲骨文服务器申请下来后，本方案无需推翻——服务器部署见 `docs/deploy-oracle.md`，两套可以并存。

---

## 一、为什么必须走隧道

本机网卡地址是 `10.50.31.86`，属于运营商级 NAT 内网段，**路由器端口映射做不了**，外网无法直接连进来。Cloudflare Tunnel 由本机主动向外拨一条长连接，绕开这个限制。

代价只有一条：**本机必须开机且联网，服务才在线。**关机、断网、休眠都会导致公网地址打不开。

---

## 二、前提条件

本机已具备（当前这台机器已验证）：

| 依赖 | 用途 | 检查命令 |
|---|---|---|
| Python 3.13 + 依赖 | Web 服务与转换引擎 | `pip install -r requirements.txt` |
| MiKTeX（xelatex） | 生成 PDF | `xelatex --version` |
| LibreOffice | tex → 可编辑 Word、取目录页码 | `soffice --version` |
| 域名托管在 Cloudflare | 隧道绑域名 | Cloudflare 控制台确认 |

换一台机器部署，上面三样都要装；缺 LibreOffice 就出不了 Word，缺 xelatex 就出不了 PDF。

---

## 三、部署步骤

### 第 1 步：改访问口令（必做）

编辑 `deploy/local/start.bat`：

```
set THESIS_AUTH_USER=thesis
set THESIS_AUTH_PASS=change-me-please
```

**一定要改。**这个服务会接收上传的 docx 并在本机编译，裸奔到公网等于把本机命令行借给陌生人。口令位数建议 12 位以上。

### 第 2 步：启动服务，先在本机验证

双击 `deploy/local/start.bat`，看到类似输出即正常：

```
Starting thesis format service on http://127.0.0.1:8000
访问口令：已开启（用户 thesis）
同时编译上限：2 个；上传上限：20 MB
```

浏览器打开 <http://127.0.0.1:8000>，输入口令应能看到上传界面。留着这个窗口别关，它就是服务进程。

### 第 3 步：建隧道并绑域名

双击 `deploy/local/install-tunnel.bat`，脚本依次做四件事：

1. 检查 cloudflared，没有就用 winget 装；
2. `cloudflared tunnel login` —— 弹出浏览器，选中你的域名站点点授权；
3. `cloudflared tunnel create thesis-format` —— 生成隧道凭证；
4. 提示你输入公网域名（例如 `thesis.你的域名.com`），自动写入配置、添加 DNS 记录、注册为 Windows 服务。

脚本里的 `config.yml` 生成在 `%USERPROFILE%\.cloudflared\config.yml`，内容形如：

```yaml
url: http://127.0.0.1:8000
tunnel: <一长串 UUID>
credentials-file: C:/Users/<你>/.cloudflared/<UUID>.json
ingress:
  - hostname: thesis.你的域名.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

### 第 4 步：公网验证

```bash
curl https://thesis.你的域名.com/api/health
```

返回 `{"ok":true,"auth":true,"running":0}` 即隧道打通。浏览器打开该地址，输入口令即可使用。

### 第 5 步：跑一遍完整自检

```bash
python deploy/local/selftest.py --base https://thesis.你的域名.com --user thesis --pass 你的口令
```

脚本会真实上传一篇 docx，走完转换、编译、下载，全程打印耗时。全部通过才说明公网链路真的可用——只看健康检查是不够的，它不碰编译。

### 第 6 步：设置开机自启

双击 `deploy/local/install-autostart.bat`，把 Web 服务注册成登录自启的定时任务。隧道已经是 Windows 服务，开机自动跑。两边都在，重启后不用手动干预。

---

## 四、日常运维

| 操作 | 命令 |
|---|---|
| 停隧道 | `net stop cloudflared`（管理员） |
| 启隧道 | `net start cloudflared`（管理员） |
| 隧道状态 | `cloudflared tunnel info thesis-format` |
| 手动起 Web 服务 | 双击 `deploy/local/start.bat` |
| 触发定时任务 | `schtasks /run /tn ThesisFormatWeb` |
| 停 Web 服务 | 关掉那个黑色窗口，或任务管理器结束 python |
| 删掉开机自启 | `schtasks /delete /tn ThesisFormatWeb /f` |

改了 `start.bat` 里的口令后，必须重启 Web 服务才生效（隧道不用动）。

---

## 五、安全须知

1. **口令必须改**，不要用默认值。
2. 想再加一层保险：在 Cloudflare Zero Trust 里给这个域名配 Access 策略（比如只允许学校邮箱、或一次性验证码）。Basic 口令是明文的，Access 是 CF 侧拦截，两者可以叠加。
3. 服务只监听 `127.0.0.1`，公网入口只有隧道这一条，本机防火墙无需额外放行。
4. 编译任务结束后临时目录会自动回收，内存中只保留最近 50 个任务记录。

---

## 六、性能与限制

- **一次构建 1–2 分钟**：XeLaTeX 要跑三遍收敛目录，再调 LibreOffice 生成 Word。同时最多 2 个任务，第 3 个会收到「已有 N 个任务在编译」的提示。
- **上传上限 20 MB**（图片多的论文注意）。改 `THESIS_MAX_UPLOAD_MB` 可放宽。
- **并发量按「几个人轮流用」设计**，不适合几十人同时提交。真有这个需求，还是得上服务器。
- 上传的论文会存进 `samples/<学校>/<论文名>/`，注意磁盘占用。

---

## 七、国内访问慢时的备选：cpolar

Cloudflare 在国内的访问速度因运营商而异。如果同事反馈打开慢或时好时坏，换国内穿透：

1. 注册 cpolar（或花生壳），拿到 authtoken；
2. 装客户端，`cpolar authtoken <你的 token>`；
3. `cpolar http 8000` 即得到一个公网地址；免费版地址每次重启会变，付费版（约 30 元/月）可固定子域名。

届时只需把隧道停掉（`net stop cloudflared`），Web 服务本身不用改——它只认 `127.0.0.1:8000`。

---

## 八、故障排查

| 现象 | 原因与处理 |
|---|---|
| 网页 502 / Bad Gateway | 隧道通了但本机服务没起，检查 `start.bat` 那个窗口是否还活着 |
| 一直弹登录框 | 口令输错；或改了 `start.bat` 后没重启服务 |
| `/api/health` 打不开 | 隧道没跑：`net start cloudflared`；或 DNS 记录没生效（等几分钟） |
| 上传报「文件超过 20 MB」 | 压缩论文里的图片，或调大 `THESIS_MAX_UPLOAD_MB` |
| 报「已有 N 个任务在编译」 | 等前一个跑完，或调大 `THESIS_MAX_JOBS`（注意 CPU 扛不扛得住） |
| 出不了 Word / PDF | 检查 LibreOffice 与 MiKTeX 是否还在，看服务端日志 |
| 关机后公网打不开 | 本方案的固有特性，开机联网即恢复 |

---

## 九、和服务器方案的关系

本机方案是「先跑起来」的过渡。等甲骨文 ARM 机器到位，按 `docs/deploy-oracle.md` 部署即可，代码同一套（`core/` + `schools/`），只是运行环境从 Windows 本机换成 Linux 容器。

迁移时唯一要留意的是字体：Linux 容器里没有宋体黑体，`format.sty` 已用 `\IfFontExistsTF` 做了 Noto 回退，部署后务必按那篇文档自检中文字体，否则会出方块。
