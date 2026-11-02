"""Content checks for local formatting, before a job can be marked complete."""
import json
import hashlib
import re
import unicodedata
from collections import Counter
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from . import docx2tex


def normalized(text):
    text = re.sub(r"\\textsuperscript\{([^}]*)\}", r"\1", text)
    text = re.sub(r"([图表])\s*\d+(?:\s*[-—－.]\s*\d+)*", r"\1", text)
    return ''.join(c for c in unicodedata.normalize('NFKC', text) if c.isalnum())


def verify(source, output, pdf_path, report_path, strict=False):
    import fitz
    source_doc = Document(source)
    items = docx2tex.read_items(source_doc)
    marks = docx2tex.locate(items)
    start = docx2tex._body_start(items, marks)
    body = [item for item in items if item['pno'] >= start]
    result = Document(output)
    word_text = normalized(''.join(node.text or '' for node in result.element.body.iter(qn('w:t'))))
    with fitz.open(pdf_path) as pdf:
        # Extract from body area only (exclude header/footer regions) to avoid false positives
        # Use same footer height calculation as toc.py for consistency
        pdf_text = normalized(''.join(page.get_text(clip=fitz.Rect(0, 0, page.rect.width, page.rect.height - 60)) for page in pdf))
        # Check for LaTeX command leakage in body area only, not in margins/footers
        # Use word boundaries to avoid matching legitimate backslashes in text
        leaked = [i + 1 for i, page in enumerate(pdf)
                  if re.search(r"\\(?:par|vspace|baselineskip|textsuperscript)\b",
                               page.get_text(clip=fitz.Rect(0, 0, page.rect.width, page.rect.height - 60)))]
        blank = [i + 1 for i, page in enumerate(pdf) if not page.get_text().strip() and not page.get_images()]
    expected = []
    ref_start = items[marks['ref']]['pno'] if marks['ref'] is not None else None
    ack_start = items[marks['ack']]['pno'] if marks['ack'] is not None else None
    for item in body:
        if item['kind'] == 'tbl':
            expected.extend(cell for row in item['rows'] for cell in row if cell.strip())
        elif item['text']:
            text = item['text']
            in_references = (ref_start is not None and item['pno'] >= ref_start
                             and not (ack_start is not None and ack_start > ref_start
                                      and item['pno'] >= ack_start))
            if in_references:
                # Bibliography labels are regenerated regardless of entry length.
                text = re.sub(r'^\s*\[\d+\]\s*', '', text)
            elif len(text) <= 100:
                text = re.sub(r'^\s*(?:[（(]\d+[)）]|\[\d+\]|\d+(?:\.\d+)*)\s*', '', text)
            expected.append(text)
    # Check chunks as PDF page breaks and table ordering can split long paragraphs.
    required = Counter(chunk for text in expected for value in [normalized(text)]
                       for chunk in [value[i:i+48] for i in range(0, len(value), 48)] if chunk)
    reference_texts = [item['text'] for item in body if item['kind'] == 'p'
                       and ref_start is not None and item['pno'] > ref_start
                       and not (ack_start is not None and ack_start > ref_start
                                and item['pno'] >= ack_start) and item['text']]
    labelled = [bool(re.match(r'^\s*\[\d+\]', text)) for text in reference_texts]
    warnings = []
    if any(labelled) and not all(labelled):
        warnings.append('原稿参考文献混用有编号和无编号条目，已统一连续编号；请核对正文引用与文献条目的对应关系。')
    missing_word = [chunk for chunk, count in required.items() if word_text.count(chunk) < count]
    missing_pdf = [chunk for chunk, count in required.items() if pdf_text.count(chunk) < count]
    expected_images = sum(len(item.get('imgs', [])) for item in body)
    expected_tables = sum(item['kind'] == 'tbl' for item in body)
    actual_images = len(result.inline_shapes)
    source_images = {str(part.partname): hashlib.sha256(part.blob).hexdigest()
                     for part in source_doc.part.related_parts.values()
                     if str(part.partname).startswith('/word/media/')}
    expected_hashes = Counter(source_images[path] for item in body
                              for path in item.get('imgs', []) if path in source_images)
    actual_hashes = Counter()
    for shape in result.inline_shapes:
        for rid in shape._inline.xpath('.//a:blip/@r:embed'):
            actual_hashes[hashlib.sha256(result.part.related_parts[rid].blob).hexdigest()] += 1
    missing_image_hashes = list((expected_hashes - actual_hashes).elements())
    actual_tables = len(result.tables)
    report = dict(missing_word=missing_word, missing_pdf=missing_pdf, warnings=warnings,
                  expected_images=expected_images, actual_images=actual_images,
                  missing_image_hashes=missing_image_hashes,
                  expected_tables=expected_tables, actual_tables=actual_tables,
                  leaked_control_pages=leaked, blank_pages=blank,
                  comparison='NFKC letters/digits, normalized figure/table references; ignores punctuation and whitespace, not character-exact')
    report['passed'] = not (missing_word or missing_pdf or missing_image_hashes or leaked or blank or
                            actual_images < expected_images or actual_tables < expected_tables)
    if not report['passed']:
        warnings.append('内容核验发现可能的差异（文字 %d 项、图片 %d 项、空白页 %d 页），结果已生成，请对照原稿检查。' %
                        (len(missing_word) + len(missing_pdf), len(missing_image_hashes), len(blank)))
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if strict and not report['passed']:
        raise ValueError('内容保全核验失败，请查看本次任务核验报告')
    return report
