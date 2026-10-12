# -*- coding: utf-8 -*-
"""毕业论文格式流水线 · 服务端到端自检。

跑一遍完整链路：健康检查 → 上传 docx 转 tex → 提交构建 → 轮询状态 → 下载 PDF/Word。
本地和公网地址都能测，部署完先跑这个确认服务真的可用。

    python deploy/local/selftest.py
    python deploy/local/selftest.py --base https://thesis.example.com --user thesis --pass 口令
    python deploy/local/selftest.py --docx 某篇论文.docx --school hkjjxy
"""
import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _auth(user, pwd):
    if not user:
        return {}
    token = base64.b64encode(("%s:%s" % (user, pwd)).encode("utf-8")).decode()
    return {"Authorization": "Basic " + token}


def _get(url, user, pwd, timeout=30):
    req = urllib.request.Request(url, headers=_auth(user, pwd))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def _post_json(url, payload, user, pwd, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(_auth(user, pwd))
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post_file(url, field, filename, blob, extra, user, pwd, timeout=120):
    b = "----selftest" + uuid.uuid4().hex
    out = bytearray()
    for k, v in extra.items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (b, k, v)).encode("utf-8")
    out += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (b, field, filename)).encode("utf-8")
    out += blob + b"\r\n"
    out += ('--%s--\r\n' % b).encode("utf-8")
    headers = {"Content-Type": "multipart/form-data; boundary=" + b}
    headers.update(_auth(user, pwd))
    req = urllib.request.Request(url, data=bytes(out), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _download(url, dest, user, pwd, timeout=120):
    req = urllib.request.Request(url, headers=_auth(user, pwd))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        blob = r.read()
    Path(dest).write_bytes(blob)
    return len(blob)


def _check_pdf_images(pdf_path, tex):
    """图片丢了 PDF 照样能出，只有数一下内嵌图片才知道真相。"""
    refs = len(re.findall(r"\\includegraphics", tex))
    if not refs:
        return True
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        n = sum(len(p.get_images(full=True)) for p in doc)
        pages = doc.page_count
        doc.close()
    except Exception as e:
        print("[6/6] 跳过图片校验：%s" % e)
        return True
    ok = n >= refs
    print("[6/6] PDF %d 页，tex 引用 %d 张图，内嵌 %d 张 → %s"
          % (pages, refs, n, "一致" if ok else "图片丢失，检查 figures 目录"))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="格式流水线端到端自检")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="服务地址，末尾不要带斜杠")
    ap.add_argument("--user", default=os.environ.get("THESIS_AUTH_USER", ""))
    ap.add_argument("--pass", dest="pwd", default=os.environ.get("THESIS_AUTH_PASS", ""))
    ap.add_argument("--school", default="hkjjxy")
    ap.add_argument("--docx", default=None, help="待上传的 docx，默认取仓库里的样例")
    ap.add_argument("--timeout", type=int, default=900, help="等待构建完成的最长秒数")
    a = ap.parse_args(argv)

    docx = a.docx
    if not docx:
        cands = sorted((ROOT / "samples" / a.school).glob("*/_source.docx"))
        if not cands:
            sys.stderr.write("找不到样例 docx，请用 --docx 指定\n")
            return 2
        docx = str(cands[0])
    if not Path(docx).is_file():
        sys.stderr.write("文件不存在：%s\n" % docx)
        return 2

    base = a.base.rstrip("/")
    print("服务地址：%s" % base)
    print("测试文件：%s（%.1f MB）" % (docx, Path(docx).stat().st_size / 1048576))

    try:
        _, health = _get(base + "/api/health", a.user, a.pwd)
        print("[1/5] 健康检查：%s" % health.decode("utf-8").strip())
    except urllib.error.HTTPError as e:
        sys.stderr.write("[1/5] 健康检查失败：HTTP %d（口令不对或隧道没通）\n" % e.code)
        return 1
    except Exception as e:
        sys.stderr.write("[1/5] 连不上服务：%s\n" % e)
        return 1

    t0 = time.time()
    try:
        _, conv = _post_file(base + "/api/convert", "file", Path(docx).name,
                             Path(docx).read_bytes(), {"school": a.school}, a.user, a.pwd)
    except urllib.error.HTTPError as e:
        sys.stderr.write("[2/5] 转换失败：HTTP %d %s\n" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 1
    print("[2/5] docx → tex 成功：%s（%.1fs，tex %d 字符）"
          % (conv["name"], time.time() - t0, len(conv["tex"])))

    try:
        _, job = _post_json(base + "/api/build",
                            {"school": a.school, "name": conv["name"],
                             "tex": conv["tex"], "pdf": True, "docx": True},
                            a.user, a.pwd)
    except urllib.error.HTTPError as e:
        sys.stderr.write("[3/5] 提交构建失败：HTTP %d %s\n" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 1
    jid = job["job"]
    print("[3/5] 已提交构建，任务号 %s" % jid)

    t1 = time.time()
    while True:
        _, raw = _get(base + "/api/job/" + jid, a.user, a.pwd)
        st = json.loads(raw.decode("utf-8"))
        if st["state"] in ("done", "error"):
            break
        if time.time() - t1 > a.timeout:
            sys.stderr.write("[4/5] 超时（%d 秒）未完成\n" % a.timeout)
            return 1
        time.sleep(3)
    if st["state"] == "error":
        print("[4/5] 构建失败，服务端日志：")
        print("\n".join(st["log"])[-3000:])
        return 1
    print("[4/5] 构建完成，用时 %.1f 秒，产物 %s" % (time.time() - t1, list(st["files"])))

    outdir = Path(tempfile.mkdtemp(prefix="thesis-selftest-"))
    pdf_path = None
    for kind in ("pdf", "docx"):
        if kind not in st["files"]:
            continue
        dest = outdir / st["files"][kind]
        size = _download("%s/api/download/%s/%s" % (base, jid, kind),
                         str(dest), a.user, a.pwd)
        if kind == "pdf":
            pdf_path = dest
        print("[5/5] 已下载 %s：%.2f MB → %s" % (kind, size / 1048576, dest))
    if pdf_path and not _check_pdf_images(pdf_path, conv["tex"]):
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
