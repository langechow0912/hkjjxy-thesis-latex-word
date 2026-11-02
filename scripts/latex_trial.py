"""Reproducible, isolated XeLaTeX trial; no AI or paid API calls."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import compiler, docx2tex, profile
from docx import Document
import fitz


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--compile-wrapper', required=True, help='Path to latex-thesis-zh/scripts/compile.py')
    args = parser.parse_args()
    source = Path(args.input).resolve()
    work = Path(args.output).resolve()
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, work / 'source.docx')
    cfg = profile.load('hkjjxy')
    logs = []
    tex, stats = docx2tex.convert(str(source), str(work), cfg, log=logs.append)
    doc = Document(source)
    items = docx2tex.read_items(doc)
    marks = docx2tex.locate(items)
    marks['body_start'] = docx2tex._body_start(items, marks)
    # Use the school template, never the uploaded manuscript's cover/signature.
    shutil.copyfile(profile.path(cfg, cfg['cover']['template']), work / 'cover.docx')
    original_pdf = compiler.convert_pdf(cfg, str(work / 'cover.docx'), str(work))
    with fitz.open(original_pdf) as pdf:
        front = fitz.open()
        front.insert_pdf(pdf, from_page=0, to_page=1)
        front.save(work / 'front.pdf')
        front.close()
    compiler.sync_format_sty(cfg, str(work))
    # Apply bibliography typography to entries, not only enumitem labels.
    tex = tex.replace(r'\begin{document}', r'\AddToHook{env/thesref/begin}{\linespread{1}\zihao{5}\songti\selectfont\sloppy\emergencystretch=3em}' + '\n' + r'\begin{document}', 1)
    (work / 'main.tex').write_text(tex, encoding='utf-8', newline='\n')
    for n in range(1, 5):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(args.compile_wrapper).resolve()),
                                 'main.tex', '--recipe', 'xelatex'],
                                cwd=work, capture_output=True, timeout=240)
        (work / f'compile-{n}.txt').write_bytes(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout.decode('utf-8', 'replace')[-3500:])
            raise RuntimeError(f'XeLaTeX pass {n} failed')
    audit(source, work, stats, logs, doc, items, marks)

def audit(source, work, stats, logs, doc, items, marks):
    pdf = fitz.open(work / 'main.pdf')
    texts = [p.get_text(clip=fitz.Rect(0, 0, p.rect.width, p.rect.height - 65)) for p in pdf]
    def norm(s):
        return ''.join(c for c in s if c.isalnum())
    alltext = norm(''.join(texts[2:]))
    missing = []
    for it in items:
        if it['kind'] == 'p' and it['pno'] >= marks['body_start'] and it['text']:
            text = re.sub(r'^\[\d+\]\s*', '', it['text'])
            text = re.sub(r'^表\s*\d+\s*[-—－]\s*\d+\s*[:：]?\s*', '', text)
            text = re.sub(r'^图\s*\d+\s*[-—－]\s*\d+\s*[:：]?\s*', '', text)
            text = re.sub(r'^[（(]\d+[)）]\s*', '', text)
            # Heading numbering is regenerated, so compare the title separately.
            text = re.sub(r'^\d+(?:\.\d+)*\s*', '', text) if len(text)<90 else text
            if norm(text) not in alltext:
                missing.append({'paragraph': it['pno'], 'text': it['text']})
    cells_missing = []
    body_tables = [it['rows'] for it in items if it['kind'] == 'tbl' and it['pno'] >= marks['body_start']]
    for ti, rows in enumerate(body_tables):
        for ri, row in enumerate(rows):
            for ci, text in enumerate(row):
                if norm(text) and norm(text) not in alltext:
                    cells_missing.append([ti, ri, ci, text])
    log = (work / 'main.log').read_text(encoding='utf-8', errors='replace')
    report = {
        'input': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'environment': 'Windows MiKTeX XeLaTeX; not a Linux server simulation',
        'cover': 'first two pages rendered from school cover-template.docx by LibreOffice; body is XeLaTeX',
        'stats': stats, 'conversion_logs': logs, 'pages':len(pdf),
        'all_pages_a4': all(abs(p.rect.width-595.276)<1 and abs(p.rect.height-841.89)<1 for p in pdf),
        'blank_pages':[i+1 for i,p in enumerate(pdf) if not p.get_text().strip() and not p.get_images()],
        'missing_body_paragraphs':missing, 'missing_table_cells':cells_missing,
        'fonts':sorted({f[3] for p in pdf for f in p.get_fonts()}),
        'warnings': [line for line in log.splitlines() if any(s in line for s in ['Overfull', 'Missing character', 'undefined references', 'Rerun to'])],
        'content_comparison': 'alphanumeric/CJK comparison; whitespace and punctuation ignored; not character-exact',
        'body_table_count':len(body_tables),
        'extracted_image_count':len(list((work / 'figures').glob('*'))),
        'ai_checks':False, 'visual_review':'not performed; automated checks only',
        'school_compliance':'partial automated checks; not a complete compliance certification',
    }
    (work / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['pages','stats','blank_pages','warnings']},ensure_ascii=True))
    print('missing paragraphs',len(missing),'missing cells',len(cells_missing))

if __name__ == '__main__':
    main()
