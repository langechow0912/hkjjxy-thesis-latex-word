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
└── docs/
    ├── new-school-guide.md     # 新增学校的 6 步操作手册
    └── lessons-learned.md      # 本轮踩坑清单（改新学校前先看）
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

## 四、已知事项

- 参考文献列表偶有 7pt 左右的轻微溢出，已用 `\emergencystretch=2em` 收敛，不影响观感
- 封面模板是**占位符版**（题目/姓名/学院等为「三号字 黑体」提示文字），需按实际信息填写
- 样例 PDF/Word 含论文全文，公开仓库前请先脱敏

## 五、版本

- **v1.0**（2026-10-10）格式包 + 《企业筹资风险研究》全链路
- **v1.1**（2026-10-12）插图 / 图目录；修 ctexart 默认页眉；修 Word 端 `\newpage` 被跳过；PDF 封面改用 Word 前 2 页
- **v2.0**（2026-10-12）重构为 `core` + `schools` 分层：学校差异全部抽到 `profile.yaml`，
  引擎只维护一份；修正文表题编号恒为 0；新增 CLI 与操作手册
