# 海口经济学院毕业论文 LaTeX 格式包（hkjjxy-format）

把学校《附件13 论文格式（修订10.21）》落成可直接编译的 LaTeX 格式包，并配套脚本产出**可编辑 Word 版**。
格式包里的每条设置都带编号（如【J-3】），与 `论文格式要求_逐条清单.md` 一一对应，改样式前先查编号。

```
hkjjxy-thesis-format/
├── hkjjxy-format.sty           # 格式包本体（真源，放于 .tex 同目录或 TEXMF）
├── 论文格式要求_逐条清单.md     # 逐条规则 + LaTeX 实现说明
├── template/                   # 学校封面模板（Word 首页 + 声明页）
├── scripts/                    # LaTeX → 可编辑 Word 的转换脚本
│   ├── build_docx.py           # 纯表格版样例（企业筹资风险研究）
│   └── build_docx_yl.py        # 含图片/图目录版样例（以岭药业）
└── samples/                    # 两篇完整样例：tex + 编译产物 PDF + Word 版
    ├── 企业筹资风险研究/
    └── 以岭药业财务风险分析/
```

## 一、环境

- TeX：MiKTeX（XeLaTeX，`-disable-installer` 关闭自动装包），需 SimSun / SimHei / KaiTi / Times New Roman
- Python 3.13：`python-docx`、`pymupdf(fitz)`
- LibreOffice（仅用于 Word → PDF 取页码 / 取封面页）

## 二、出 PDF

```bash
xelatex -interaction=nonstopmode -halt-on-error -disable-installer main.tex   # 连跑 2~3 遍
```

- 封面与声明页**不进 LaTeX 排版**：先按学校模板生成 Word 前 2 页 → 转 PDF 取前 2 页存为 `front.pdf` →
  `main.tex` 开头用 `\includepdf[pages={1,2},pagecommand={}]{front.pdf}` 嵌入。
  这样 PDF 封面与 Word 完全一致（不要用源稿 docx 直接转，LibreOffice 渲染会把声明页挤到封面上）。
- 页码：`\pagestyle{plain}`，页码在页脚居中【J-3】；封面/声明/目录首页单独 `\thispagestyle{empty}`。

## 三、出可编辑 Word

`scripts/build_docx*.py` 直接解析 `main.tex` 结构写 docx（不是 PDF 转换），保持表格/图片/编号域可编辑：

1. 改脚本顶部 `TEX` / `OUT_DIR` / `OUT` / `TPL` / `FIG_DIR` 五个路径
2. `python build_docx_yl.py`
3. 脚本自动跑两遍：第一遍生成探针 docx → LibreOffice 渲染 → fitz 取真实页码 → 第二遍把页码回填进目录域（Word 里仍可 F9 更新）

细节约定：

- 表题用 `SEQ 表` 域、图题用 `SEQ 图` 域，增删图表后全选 F9 即可重新编号
- 正文里的 `\newpage` 会转成真正的分页符（摘要→引言、结论→参考文献另起一页）
- 三线表、图题黑体五号居中于图下方，均由脚本按格式要求写入

## 四、已知事项

- 参考文献列表偶有 7pt 左右的轻微溢出，已用 `\emergencystretch=2em` 收敛，不影响观感
- 封面模板是**占位符版**（题目/姓名/学院等为"三号字 黑体"提示文字），需按实际信息填写
- 样例 PDF/Word 含学生姓名与论文正文，公开仓库前请先脱敏

## 五、版本

- v1.0（2026-10-10）完成格式包与《企业筹资风险研究》全链路
- v1.1（2026-10-12）新增插图/图目录支持；修正 ctexart 默认页眉（改为无页眉、页码页脚居中）；修正 Word 端 `\newpage` 被跳过导致的引言不分页；PDF 封面改用 Word 前 2 页
