# -*- coding: utf-8 -*-
"""学校格式档案（profile.yaml）加载与单位换算。

职责边界：
  profile.yaml   数值真源 —— 页边距、字号、行距、标题格式、图表题格式等
  format.sty     LaTeX 侧实现 —— 与 profile.yaml 的 page/body/heading/table/
                 figure/toc 各节数值一一对应，两份文件头部都有对照说明
  docxbuild.py   Python 侧实现 —— 直接读 profile，不硬编码任何学校数值

改一所学校的格式时：先改 profile.yaml，再同步 format.sty 里带【】编号的对应行。
"""
import io
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHOOLS = os.path.join(ROOT, "schools")
SAMPLES = os.path.join(ROOT, "samples")

#: 中文字号 → pt。profile.yaml 里建议直接写 pt 数值，注释里标注对应中文字号。
ZIHao = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22, "小二": 18,
    "三号": 16, "小三": 15, "四号": 14, "小四": 12, "五号": 10.5, "小五": 9,
    "六号": 7.5, "小六": 6.5,
}


def load(school_id):
    """读取 schools/<school_id>/profile.yaml"""
    p = os.path.join(SCHOOLS, school_id, "profile.yaml")
    if not os.path.isfile(p):
        raise SystemExit("找不到学校档案：%s" % p)
    with io.open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("id", school_id)
    cfg["_dir"] = os.path.join(SCHOOLS, school_id)
    cfg["_file"] = p
    return cfg


def path(cfg, value):
    """解析路径模板。可用变量：{root} 仓库根、{school} schools/<id>"""
    return os.path.normpath(str(value).format(root=ROOT, school=cfg["_dir"]))


def pt(size):
    """把 profile 里的字号转成 pt：数字视为 pt，中文号数查表"""
    if isinstance(size, (int, float)):
        return float(size)
    return float(ZIHao[str(size).strip()])


def textwidth_pt(cfg):
    """版心宽度（pt）= 纸宽 − 左右边距"""
    page = cfg["page"]
    return mm(page["width"]) - mm(page["margins"]["left"]) - mm(page["margins"]["right"])


def _mm(v):
    """带单位的尺寸 → 毫米数。支持 210mm / 25mm / 纯数字（按 mm 计）"""
    s = str(v).strip().lower()
    if s.endswith("mm"):
        return float(s[:-2])
    if s.endswith("cm"):
        return float(s[:-2]) * 10.0
    if s.endswith("pt"):
        return float(s[:-2]) / 72.0 * 25.4
    return float(s)


def mm(v):
    """毫米 → 磅"""
    return _mm(v) / 25.4 * 72.0
