# -*- coding: utf-8 -*-
"""把识别结果写回 Word：给每个标题重新打上 Word 标题样式（标题 1/2/3/4）。

云端排版服务（大以论文）只收一份 Word，自己从文档里认标题；原稿标题常常是加粗短行、
手打编号、样式乱用，它就容易认错。这里用我们自己的识别结果（规则 + 用户在预览里确认 / AI 助手
改过的）把每个标题的级别明确写进文档，再交给对方，让它不用猜。

只改段落样式和大纲级别，不改文字（用户在预览里手改过标题文字的除外）、不动图表、不动页面设置。
"""
import io

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from . import docx2tex

MAX_LEVEL = 4


def _heading_style(doc, level):
    """文档里已有的「Heading n / 标题 n」就用它，没有就建一个最小的。"""
    for name in ("Heading %d" % level, "标题 %d" % level):
        try:
            return doc.styles[name]
        except KeyError:
            continue
    style = doc.styles.add_style("Heading %d" % level, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = doc.styles["Normal"]
    style.next_paragraph_style = doc.styles["Normal"]
    pPr = style.element.get_or_add_pPr()
    ol = OxmlElement("w:outlineLvl")
    ol.set(qn("w:val"), str(level - 1))
    pPr.append(ol)
    style.font.bold = True
    return style


def _clear_direct_outline(paragraph):
    pPr = paragraph._p.pPr
    if pPr is None:
        return
    for el in pPr.findall(qn("w:outlineLvl")):
        pPr.remove(el)


def _set_text(paragraph, text):
    """改标题文字：保留第一个 run 的格式，其余 run 清空"""
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for run in runs[1:]:
        run._r.getparent().remove(run._r)


def retag(source, overrides=None):
    """source 是 docx 的路径或字节。返回 (新 docx 字节, 说明 dict)。

    说明：retagged 重新打了样式的标题数，changed 其中级别和原稿不同的数，
    demoted 被取消标题样式的段落数，retexted 改了文字的标题数。
    """
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(bytes(source))
    doc = Document(source)
    items = docx2tex.read_items(doc)
    marks = docx2tex.locate(items)
    marks["body_start"] = docx2tex._body_start(items, marks)
    clean, _ = docx2tex.normalize_overrides(overrides, items) if overrides else ([], [])
    figmap = {}
    for it in items:
        for target in it.get("imgs") or ():
            figmap[target.split("/")[-1]] = target.split("/")[-1]
    trace = {}
    docx2tex.scan(items, marks, figmap, clean or None, trace)

    paras = [Paragraph(child, doc) for child in doc.element.body.iterchildren()
             if child.tag.split("}")[-1] == "p"]
    texts = {o["idx"]: o["text"] for o in clean if o.get("text")}
    forced_body = {o["idx"] for o in clean if o.get("role") == "body"}

    report = {"retagged": 0, "changed": 0, "demoted": 0, "retexted": 0}
    for idx, it in enumerate(items):
        if it["kind"] != "p" or it.get("from_table") or it["pno"] >= len(paras):
            continue
        tr = trace.get(idx) or {}
        para = paras[it["pno"]]
        role, level = tr.get("role"), tr.get("level")
        if role == "heading" and level and level <= MAX_LEVEL:
            _clear_direct_outline(para)
            para.style = _heading_style(doc, level)
            report["retagged"] += 1
            if it.get("heading_level") != level:
                report["changed"] += 1
            if idx in texts and texts[idx] != it["text"]:
                _set_text(para, texts[idx])
                report["retexted"] += 1
        elif it.get("heading_level") and (role == "demoted" or idx in forced_body):
            # 原稿套了标题样式，但识别 / 用户判定它不是标题：取消，免得对方把它当标题
            _clear_direct_outline(para)
            para.style = doc.styles["Normal"]
            report["demoted"] += 1

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), report
