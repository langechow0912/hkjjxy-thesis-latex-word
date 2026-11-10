# -*- coding: utf-8 -*-
"""图片与题注的识别辅助。

题注位置在用户稿里没有统一写法：图下方（模板要求）、图上方、和图在同一段。
这里只放与文档结构无关的小工具，决策逻辑在 docx2tex.scan 里，和表题放在一起。
"""
from docx.oxml.ns import qn

_VML_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def image_targets(element, doc):
    """元素（段落或表格）里所有图片的 part 名，按出现顺序。

    DrawingML（a:blip，含浮动锚点、文本框里的图）和旧式 VML（v:imagedata）都收。
    外链图片（r:link）在包里没有对应的 part，取不到就跳过，由调用方告知用户。
    """
    out = []
    related = doc.part.related_parts
    for el in element.iter():
        if el.tag == qn("a:blip"):
            rid = el.get(qn("r:embed")) or el.get(qn("r:link"))
        elif el.tag == _VML_IMAGEDATA:
            rid = el.get(_REL_ID)
        else:
            continue
        if rid and rid in related:
            out.append(str(related[rid].partname))
    return out


def next_nonempty(items, i):
    """items[i] 之后第一个非空条目（跳过空段落）。没有返回 None。"""
    j = i + 1
    while j < len(items):
        it = items[j]
        if it["kind"] == "p" and not it["text"] and not it.get("imgs"):
            j += 1
            continue
        return it
    return None
