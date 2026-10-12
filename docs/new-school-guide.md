# 新增一所学校：操作手册

核心思路：**格式包分学校，引擎共用**。一所学校 = `schools/<id>/` 里的一组数值 + 封面模板，
`core/` 一行都不用改。若发现必须改 `core/`，说明有差异没抽成数值，先想想怎么参数化。

---

## 一、30 秒上手

```bash
cd thesis-format
python -m core.build --school hkjjxy --sample "企业筹资风险研究"
```

一条命令出两件东西：

| 产物 | 位置 |
|---|---|
| 可编辑 Word | `samples/<school>/<论文名>/<论文名>-毕业论文（可编辑）.docx` |
| PDF | `samples/<school>/<论文名>/main.pdf` |

常用参数：

```bash
--suffix v2          # 输出文件名加后缀，避免 Word 占用导致覆盖失败
--no-docx            # 只出 PDF
--no-pdf             # 只出 Word
--tmp <目录>          # 指定探针文件临时目录
```

---

## 二、目录结构

```
thesis-format/
├── core/                       # 共用引擎（新增学校时不要改）
│   ├── build.py                #   CLI 总入口
│   ├── texparse.py             #   tex → 中间结构
│   ├── docxbuild.py            #   中间结构 → docx
│   ├── toc.py                  #   目录页码探测（两遍构建）
│   ├── compiler.py             #   同步 sty / 抽取封面页 / 调 XeLaTeX
│   ├── profile.py              #   profile.yaml 加载与单位换算
│   └── tools.py                #   xelatex / soffice 路径解析
│
├── schools/<id>/               # ← 每所学校一份
│   ├── profile.yaml            #   数值真源（Python 侧直接读）
│   ├── format.sty              #   LaTeX 实现（与 profile 同步）
│   ├── rules.md                #   逐条规则 + 编号对照
│   └── cover-template.docx     #   封面 + 声明页模板
│
└── samples/<id>/<论文名>/      # 每篇论文：main.tex + figures/ + 产物
```

---

## 三、新增学校的 6 步

### 1. 复制样板
```bash
cp -r schools/hkjjxy schools/<新的学校代号>
```

### 2. 改 `profile.yaml`
至少改这几项：`school.name`、`school.basis`、`latex.package_name`、
`page.margins`、`body.size`、`heading.levels`、`toc.depth`、`cover.pages`。

字段说明见下一节。

### 3. 改 `format.sty`
按 `rules.md` 里的编号，把 sty 中带【】标记的行逐一改成新数值。
**对照表见第五节**，照着逐条走不容易漏。

改完顺手把 sty 头部的注释也更新（学校名、依据文件名）。

### 4. 换 `cover-template.docx`
找学校发的 Word 封面模板，只保留**前 N 页**（封面 + 声明），
删掉多余的空白页，另存为 `cover-template.docx`。

要点：
- 页数要和 `cover.pages` 对上（通常是 2）
- 模板里的题目/姓名等建议**保留占位文字**（如「（三号字 黑体）」），
  生成时统一，最后再按学生信息批量填
- 别用源稿论文自己的封面（那是已经填好的）

### 5. 放论文
```bash
mkdir -p samples/<id>/<论文名>
# 把 main.tex 和 figures/ 放进去
```

`main.tex` 可以直接复制现有样例再改内容。引擎识别的命令清单见第六节。

### 6. 跑 + 验收
```bash
python -m core.build --school <id> --sample <论文名>
```
然后按 `lessons-learned.md` 文末的**验收清单**逐条打勾。

---

## 四、profile.yaml 字段

```yaml
school:  {id, name, basis}                  # 代号、全称、依据文件

latex:                                      # LaTeX 侧约定
  package_name: hkjjxy-format               # tex 里 \usepackage{<这里>}
  main: main.tex
  engine: xelatex
  passes: 3

page:
  width: 210mm                              # 支持 mm / cm / pt / 纯数字(按 mm)
  height: 297mm
  margins: {top: 30mm, bottom: 30mm, left: 25mm, right: 20mm}
  pagenum: {position: footer, align: center}
  body_page_start: 1                        # 正文节页码重新起算

body: {font_cn: 宋体, font_en: Times New Roman, size: 12,
       linespread: 1.5, first_indent: 2}   # first_indent 单位是「字符」

heading:
  levels: {1: {font: 黑体, size: 15, before: 12, after: 6}, ...}
  unnum:  {font: 黑体, size: 12, before: 54, after: 6}   # 参考文献/致谢

abstract: {font: 楷体, title_font: 黑体, title_size: 18,
           en_title_size: 15, label_size: 12, body_size: 10.5}

table:
  list_title: 表目录
  caption: {position: above, font: 黑体, size: 10.5, bold: true,
            align: center, before: 4, after: 2, linespread: 1.0}
  body: {size: 10.5, linespread: 1.0, first_col_align: left}
  note: {size: 9}
  toprule_bp: 1.5
  midrule_bp: 0.5

figure:
  dir: figures
  default_width_frac: 0.82
  list_title: 图目录
  caption: {position: below, ...}

reference: {size: 10.5, linespread: 1.2, hanging: 24}
toc:       {title: 目　录, title_size: 18, depth: 3, leader: dots,
            entry_size: 12, entry_font: 宋体, linespread: 1.5,
            front_entries: [摘要, 关键词], loose_match: [参考文献, 致谢]}
cover:     {template: "{school}/cover-template.docx", pages: 2,
            front_pdf: front.pdf}
output:    {docx_name: "{sample}-毕业论文（可编辑）{suffix}.docx"}
```

`size` 可以写数字（pt）或中文号数（`小三`、`五号`），内部会查表换算。

---

## 五、profile.yaml ↔ format.sty 对照表

改学校时，左边改完，右边对应的行也要改。

| profile.yaml | format.sty 里的位置 | 规则编号 |
|---|---|---|
| `page.margins` | `\geometry{top=…,bottom=…,left=…,right=…}` | A-1 / A-2 |
| `page.pagenum` | `\pagestyle{plain}` | J-3 |
| `body.size` / `body.linespread` | `\linespread{1.5}` + 正文 `\zihao{-4}` | C-6.4 |
| `body.first_indent` | `\setlength{\parindent}{2\ccwd}` | C-6.4 |
| `heading.levels.1` | `\ctexset{section=…}` | C-7 |
| `heading.levels.2` | `\ctexset{subsection=…}` | C-7 |
| `heading.levels.3` | `\ctexset{subsubsection=…}` | C-7 |
| `heading.unnum` | `\unnumsec` 定义 | M |
| `toc.depth` | `\setcounter{tocdepth}{3}` | C-1.1 |
| `toc.entry_size` / `entry_font` | `\cftsecfont` / `\cfttabfont` / `\cftfigfont` | C-1 |
| `toc.title` / `title_size` | `\contentsname` / `\cfttoctitlefont` | C-1 |
| `table.toprule_bp` / `midrule_bp` | `\heavyrulewidth` / `\lightrulewidth` | D-2 / D-3 |
| `table.caption` | `thesTab` 环境内的表题输出 | D-4 |
| `table.note.size` | `\tabnote` 里的 `\zihao{-5}` | D-7 |
| `figure.caption` | `\captionsetup[figure]{…}` | E-2 / E-3 |
| `reference.*` | `\setlist[thesref]{…}` | C-9 |
| `latex.package_name` | tex 里的 `\usepackage{…}` | — |

---

## 六、引擎识别的命令清单

`main.tex` 里用这些命令，引擎才能正确解析。新学校**沿用这套命名**最省事，
改名字就要动 `core/texparse.py`（不推荐）。

| 命令 | 作用 | 产物 |
|---|---|---|
| `\cnTitle{}` `\cnSubTitle{}` `\enTitle{}` | 中英文题目 | 封面信息 + 正文题目 |
| `\cnAbstract{%…}` `\cnKeywords{}` | 中文摘要/关键词 | 正文 + 目录条目 |
| `\enAbstract{%…}` `\enKeywords{}` | 英文摘要/关键词 | 正文 |
| `\section{}` `\subsection{}` `\subsubsection{}` | 一~三级标题 | 自动编号 + 目录 |
| `\paragraph*{}` | 四级标题 | 加粗段首，不进目录 |
| `\unnumsec{}` | 无编号一级块 | 参考文献 / 致谢 |
| `\begin{thesTab}[htbp]{表名\label{tab:tN}}{表注}` | 一体化三线表 | 表题 + 表体 + 表注 |
| `\begin{figure}…\includegraphics[width=0.82\linewidth]{…}…\caption{}` | 插图 | 图 + 图题 |
| `\begin{thesref}…\item …` | 参考文献列表 | `[1] …` 悬挂缩进 |
| `\newpage` | 分页 | 转成真正的分页符 |
| `\textsuperscript{[n]}` | 引用上标 | 上标 |

---

## 七、常见改动场景

**改正文字号**：只改 `profile.yaml` 的 `body.size`（Word 侧立即生效）；
LaTeX 侧还要改 `format.sty` 里的 `\zihao{-4}` 和 `\documentclass` 的 `12pt`。

**目录只收到二级**：改 `toc.depth: 2`（Word 侧）+ `\setcounter{tocdepth}{2}`（LaTeX 侧）。

**换封面**：替换 `cover-template.docx`，确认页数与 `cover.pages` 一致。

**表题改到表下方**：改 `table.caption.position: below`，两侧都改。

**加英文封面页**：`cover.pages` 改成 3，模板前 3 页对应「英文封面 + 封面 + 声明」。

---

## 八、回归基准

现有两篇样例跑通后的基准值，改格式包后用来比对：

| 样例 | PDF 页数 | 表 | 图 | 目录条目 | 编译提示 |
|---|---|---|---|---|---|
| 企业筹资风险研究 | 17 | 5 | 0 | 35 + 表5 = 40 | 0 条 |
| 以岭药业财务风险分析 | 26 | 5 | 6 | 40 + 表5 + 图6 = 51 | 2 条（参考文献轻微溢出） |
