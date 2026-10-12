# 毕业论文格式流水线（LaTeX 格式包 + 可编辑 Word）

把学校的论文格式规范落成可直接编译的 LaTeX 格式包，并配套引擎产出**可编辑 Word 版**。
一条命令同时出 PDF 和可编辑 docx，两边版式一致。

```
thesis-format/
├── core/                       # 共用引擎（与学校无关）
│   ├── build.py                #   CLI 总入口
│   ├── texparse.py             #   tex → 中间结构
│   ├── docxbuild.py            #   中间结构 → docx
│   ├── toc.py                  #   目录页码探测（两遍构建）
│   ├── compiler.py             #   同步 sty / 抽取封面页 / 调 XeLaTeX
│   ├── profile.py              #   profile.yaml 加载与单位换算
│   └── tools.py                #   xelatex / soffice 路径解析
│
├── schools/hkjjxy/             # 海口经济学院
│   ├── profile.yaml            #   数值真源（Python 侧直接读）
│   ├── format.sty              #   LaTeX 实现（与 profile 同步）
│   ├── rules.md                #   逐条规则 + 编号对照
│   └── cover-template.docx     #   封面 + 声明页模板
│
├── samples/hkjjxy/             # 两篇完整样例（tex + figures + 产物）
│   ├── 企业筹资风险研究/
│   └── 以岭药业财务风险分析/
│
├── web/                        # FastAPI 后端 + 单文件前端（无 CDN 依赖）
├── deploy/
│   ├── Caddyfile               #   反向代理 + 自动 HTTPS
│   └── local/                  #   本机部署：启动 / 隧道 / 自检脚本
├── docker-compose.server.yml   # 服务器部署（应用 + Caddy）
│
└── docs/
    ├── new-school-guide.md     # 新增学校的 6 步操作手册
    ├── lessons-learned.md      # 本轮踩坑清单（改新学校前先看）
    ├── deploy-local-tunnel.md  # 本机部署 + Cloudflare Tunnel 公网访问
    └── deploy-oracle.md        # Oracle Cloud 免费 ARM 实例部署步骤
```

## 一、环境

- **TeX**：MiKTeX（XeLaTeX，`-disable-installer` 关掉自动装包），需 SimSun / SimHei / KaiTi / Times New Roman
- **Python 3.13**：`python-docx`、`pymupdf(fitz)`、`pyyaml`
- **LibreOffice**：仅用于 Word → PDF（取页码、取封面页）

工具路径优先读 `profile.yaml` 的 `tools` 段，其次是环境变量 `THESIS_XELATEX` / `THESIS_SOFFICE`，
最后是常见安装位置自动探测。

## 二、一条命令出 Word + PDF

```bash
python -m core.build --school hkjjxy --sample "企业筹资风险研究"
```

| 参数 | 说明 |
|---|---|
| `--suffix v2` | 输出文件名加后缀（Word 占用时会覆盖失败） |
| `--no-docx` | 只出 PDF |
| `--no-pdf` | 只出 Word |
| `--tmp <目录>` | 指定探针文件临时目录 |

内部流程：
1. 读 `schools/<id>/profile.yaml`
2. 解析 `samples/<id>/<论文名>/main.tex`
3. pass1 生成探针 docx → LibreOffice 渲染 → 读回每个标题的真实页码
4. pass2 用真页码生成正式 docx（目录是 TOC 域，F9 可重算）
5. 正式 docx 前 N 页 → `front.pdf`
6. 同步 `format.sty` → XeLaTeX ×3 → `main.pdf`

## 三、两侧的职责边界

| 层 | 文件 | 内容 |
|---|---|---|
| 数值真源 | `schools/<id>/profile.yaml` | 页边距、字号、行距、标题格式、图表题格式 |
| LaTeX 侧 | `schools/<id>/format.sty` | 与 profile 逐条同步，行尾带【编号】 |
| Python 侧 | `core/docxbuild.py` | 只从 profile 取值，不含任何学校专属数值 |
| 规则文档 | `schools/<id>/rules.md` | 条款与编号一一对应 |

**改格式的顺序**：先改 `profile.yaml` → 再同步 `format.sty` 里对应编号的行 → 重跑 build → 对照回归基准。

## 四、Web 界面

浏览器里上传 Word 稿 → 自动转 LaTeX → 在线校对 → 生成并下载 PDF / Word。

**本地直接跑**

```bash
pip install -r requirements.txt
python web/app.py                 # 打开 http://127.0.0.1:8000
```

**Docker（本机部署）**

```bash
docker compose up -d              # 首次会构建镜像，约 10~20 分钟
docker compose logs -f            # 看日志
docker compose down               # 停止
```

**本机部署 + 公网访问（Cloudflare Tunnel，免费 HTTPS）**

不需要公网 IP：本机跑服务，隧道主动向外拨一条长连接，把 `127.0.0.1:8000` 映射到你自己的域名。

```bash
deploy\local\start.bat            # 1) 先改脚本里的口令，再双击启动服务
deploy\local\install-tunnel.bat   # 2) 建隧道、绑域名、注册为 Windows 服务
deploy\local\install-autostart.bat  # 3) 可选：开机自动启动 Web 服务

python deploy/local/selftest.py --base https://thesis.你的域名.com \
       --user 用户名 --pass 口令      # 4) 跑一遍完整自检
```

完整步骤、运维命令、国内访问慢时改 cpolar 的备选方案见
**[docs/deploy-local-tunnel.md](docs/deploy-local-tunnel.md)**。

**服务器部署（Oracle Cloud 免费 ARM + HTTPS）**

```bash
cp .env.example .env && nano .env        # 填域名
docker compose -f docker-compose.server.yml up -d --build
```

完整步骤（开实例、放行 80/443、装 Docker、DNS、自检、加访问密码）见
**[docs/deploy-oracle.md](docs/deploy-oracle.md)**。

注意两点：

- 镜像约 **3~4 GB**（TeX Live + LibreOffice 都是大件），磁盘要留够
- 中文字体**跨平台自动回退**：`format.sty` 用 `\IfFontExistsTF` 依次探测——
  Windows 用 SimSun / SimHei / KaiTi / FangSong；Linux 容器里没有这些字体（且不打包进镜像，版权原因），
  自动回退到镜像里预装的开源字体：Noto Serif CJK SC（≈宋体）、Noto Sans CJK SC（≈黑体）、
  AR PL UKai CN（≈楷体）、Liberation Serif（≈Times New Roman）。
  本机目录 `docker-compose.yml` 仍会挂载宿主机 `C:\Windows\Fonts`，挂了就优先用 Windows 字体。

| 接口 | 说明 |
|---|---|
| `GET /api/schools` | 学校列表（读 `schools/` 目录） |
| `POST /api/convert` | 上传 .docx → 返回 tex 源码 |
| `POST /api/build` | 提交构建，返回 job id |
| `GET /api/job/{id}` | 任务状态与日志 |
| `GET /api/download/{id}/{pdf\|docx}` | 下载产物 |
| `GET /api/health` | 健康检查（免口令，隧道探活用） |

公网暴露前用环境变量加固，都在 `deploy/local/start.bat` 里设置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `THESIS_AUTH_USER` / `THESIS_AUTH_PASS` | 空 | 设了就开启 HTTP Basic 口令；留空则任何人都能访问 |
| `THESIS_MAX_JOBS` | 2 | 同时编译上限，超了返回 503 让对方排队 |
| `THESIS_MAX_UPLOAD_MB` | 20 | 上传 docx 大小上限 |
| `THESIS_KEEP_JOBS` | 50 | 内存中保留的任务数，超出自动回收临时目录 |

## 五、已知事项

- 参考文献列表偶有 7pt 左右的轻微溢出，已用 `\emergencystretch=2em` 收敛，不影响观感
- 封面模板是**占位符版**（题目/姓名/学院等为「三号字 黑体」提示文字），需按实际信息填写
- 样例 PDF/Word 含论文全文，公开仓库前请先脱敏
- docx → tex 是**启发式**转换：靠编号识别标题，无编号的标题识别不出来，必须人工校对
- 图片在**上传转换**那一步落到 `<论文目录>/figures/`。构建时若换了论文名，服务端会尝试把
  图片补回去；补不回就直接报错，不会产出「成功但没有图」的 PDF

## 六、版本

- **v1.0**（2026-10-10）格式包 + 《企业筹资风险研究》全链路
- **v1.1**（2026-10-12）插图 / 图目录；修 ctexart 默认页眉；修 Word 端 `\newpage` 被跳过；PDF 封面改用 Word 前 2 页
- **v2.0**（2026-10-12）重构为 `core` + `schools` 分层：学校差异全部抽到 `profile.yaml`，
  引擎只维护一份；修正文表题编号恒为 0；新增 CLI 与操作手册
- **v2.1**（2026-10-12）服务器部署：字体跨平台回退（Windows 字体 ↔ Noto CJK 开源字体）、
  Caddy 自动 HTTPS、`docs/deploy-oracle.md` 部署手册
- **v2.2**（2026-10-12）本机部署 + 公网访问：HTTP Basic 口令、并发与上传上限、任务自动回收，
  `deploy/local/` 启动与隧道脚本、端到端自检脚本、`docs/deploy-local-tunnel.md`
