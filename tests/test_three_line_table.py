# -*- coding: utf-8 -*-
"""三线表：表头行自带顶线并在跨页时重复，数据行不被拆开。"""
import unittest

from docx import Document
from docx.oxml.ns import qn

from core import docxbuild

CFG = {"table": {"toprule_bp": 1.5, "midrule_bp": 0.5,
                 "body": {"size": 10.5, "linespread": 1.0, "first_col_align": "left"}}}


def build(rows):
    doc = Document()
    return docxbuild.three_line_table(doc, rows, CFG)


class ThreeLineTable(unittest.TestCase):
    def test_header_row_repeats_across_pages_and_has_its_own_top_rule(self):
        table = build([["项目", "金额"], ["甲", "1"], ["乙", "2"]])
        header = table.rows[0]._tr
        self.assertIsNotNone(header.trPr.find(qn("w:tblHeader")))
        for cell in table.rows[0].cells:
            borders = cell._tc.tcPr.find(qn("w:tcBorders"))
            top, bottom = borders.find(qn("w:top")), borders.find(qn("w:bottom"))
            self.assertEqual(top.get(qn("w:sz")), "12")        # 1.5bp = 12/8pt，与表顶线同粗
            self.assertEqual(bottom.get(qn("w:sz")), "4")      # 栏目线仍是细线

    def test_only_the_header_repeats_and_no_row_splits(self):
        table = build([["项目", "金额"], ["甲", "1"], ["乙", "2"]])
        repeats = [row._tr.trPr.find(qn("w:tblHeader")) is not None for row in table.rows]
        self.assertEqual(repeats, [True, False, False])
        for row in table.rows:
            self.assertIsNotNone(row._tr.trPr.find(qn("w:cantSplit")))

    def test_single_row_table_is_not_marked_as_repeating_header(self):
        table = build([["只有一行", "x"]])
        self.assertIsNone(table.rows[0]._tr.trPr.find(qn("w:tblHeader")))

    def test_table_level_borders_are_still_top_and_bottom_only(self):
        table = build([["a", "b"], ["c", "d"]])
        borders = table._tbl.tblPr.find(qn("w:tblBorders"))
        vals = {child.tag.split("}")[1]: child.get(qn("w:val")) for child in borders}
        self.assertEqual((vals["top"], vals["bottom"]), ("single", "single"))
        for name in ("left", "right", "insideH", "insideV"):
            self.assertEqual(vals[name], "none")


if __name__ == "__main__":
    unittest.main()
