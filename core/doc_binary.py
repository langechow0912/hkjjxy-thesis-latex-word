"""从 Word 97-2003（.doc）二进制里快速取正文字数，只用来报价。

为什么不转换：转成 .docx 需要 LibreOffice（阿里云这台没有，AWS 要跨境调用），.doc 又常常很大，
报价要快；直接读二进制只要毫秒级。实测在 5 份文档上和 .docx 的数法相差 0%～0.3%。

原则：**读不懂也不让报价失败，宁可少收**：
  1. 正常：按 [MS-DOC] 的 FIB → CLX → piece table 取正文，去掉域代码和控制字符，数非空白字符。
  2. 容器能打开但读不出正文（加密、很老的 Word 6/95、结构异常）：按正文流的大小估算，系数偏大、
     扣掉固定开销，估出来比真实字数少 16%～51%，不会多收。
  3. 连容器都打不开（RTF、网页另存的 .doc 等）：按文件大小粗估，同样偏低，并设上限。
返回 (字数, basis)：basis 是 "doc"（二进制解析）或 "estimate"（估算）。
"""
import io
import logging
import struct

import olefile

logger = logging.getLogger(__name__)

STREAM_OVERHEAD = 20000        # 一份 .doc 的正文流里和字数无关的固定开销（样式、字体表等）
STREAM_BYTES_PER_CHAR = 6.5    # 实测 3.6～6.5 字节/字，取最大值，估出来偏低
FILE_BYTES_PER_CHAR = 16       # 打不开容器时的粗估：每 16 字节算 1 个字，偏低
FALLBACK_MIN, FALLBACK_MAX = 100, 60000
MAX_FIELD_NESTING = 64


class DocParseError(Exception):
    pass


def _u16(buf, pos):
    return struct.unpack_from("<H", buf, pos)[0]


def _u32(buf, pos):
    return struct.unpack_from("<I", buf, pos)[0]


def main_text(data):
    """返回正文原始文本（含控制字符）。任何不认识的结构都抛 DocParseError。"""
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        word = ole.openstream("WordDocument").read()
        if _u16(word, 0) != 0xA5EC:
            raise DocParseError("不是 Word 97-2003 文档")
        if _u16(word, 2) < 0x00C1:
            raise DocParseError("Word 6/95 老格式")
        flags = _u16(word, 0x0A)
        if flags & 0x0100:
            raise DocParseError("文档已加密")
        table = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()
        csw = _u16(word, 32)
        pos = 34 + csw * 2
        cslw = _u16(word, pos)
        lw = pos + 2
        ccp_text = struct.unpack_from("<i", word, lw + 12)[0]
        pos2 = lw + cslw * 4
        if _u16(word, pos2) < 34:
            raise DocParseError("FIB 太短，没有 CLX")
        fc_clx, lcb_clx = struct.unpack_from("<II", word, pos2 + 2 + 33 * 8)
        if not lcb_clx or ccp_text < 0:
            raise DocParseError("没有文本描述表")
        clx = table[fc_clx:fc_clx + lcb_clx]
        i = 0
        while clx[i] == 1:                                   # 跳过 Prc
            i += 3 + _u16(clx, i + 1)
        if clx[i] != 2:
            raise DocParseError("找不到 piece table")
        lcb = _u32(clx, i + 1)
        plc = clx[i + 5:i + 5 + lcb]
        n = (lcb - 4) // 12
        if n <= 0:
            raise DocParseError("piece table 为空")
        cps = struct.unpack_from("<%dI" % (n + 1), plc, 0)
        pcds = plc[(n + 1) * 4:]
        parts, total = [], 0
        for k in range(n):
            fc = _u32(pcds, k * 8 + 2)
            compressed = bool(fc & 0x40000000)
            fc &= 0x3FFFFFFF
            length = cps[k + 1] - cps[k]
            if length < 0:
                raise DocParseError("piece 长度为负")
            if compressed:
                parts.append(word[fc // 2:fc // 2 + length].decode("cp1252", "replace"))
            else:
                parts.append(word[fc:fc + length * 2].decode("utf-16le", "replace"))
            total += length
            if total >= ccp_text:
                break
        return "".join(parts)[:ccp_text]
    except DocParseError:
        raise
    except (OSError, struct.error, IndexError, ValueError, KeyError) as exc:
        raise DocParseError("%s: %s" % (type(exc).__name__, exc)) from None


def clean(raw):
    """去掉域指令（\\x13 指令 \\x14 结果 \\x15 里的指令部分）和所有控制字符（段落标记、单元格标记、图片锚点等）。"""
    out, stack = [], []
    for ch in raw:
        if ch == "\x13":
            if len(stack) < MAX_FIELD_NESTING:
                stack.append("instr")
        elif ch == "\x14":
            if stack:
                stack[-1] = "result"
        elif ch == "\x15":
            if stack:
                stack.pop()
        elif "instr" in stack or ord(ch) < 0x20:
            continue
        else:
            out.append(ch)
    return "".join(out)


def estimate(data):
    """读不出正文时的估算，偏低。"""
    try:
        size = olefile.OleFileIO(io.BytesIO(data)).get_size("WordDocument")
        return max(FALLBACK_MIN, int((size - STREAM_OVERHEAD) / STREAM_BYTES_PER_CHAR))
    except Exception:                                        # noqa: BLE001  容器都打不开
        return min(FALLBACK_MAX, max(FALLBACK_MIN, len(data) // FILE_BYTES_PER_CHAR))


def count(data):
    """返回 (字数, basis)。不会因为读不懂文件而抛异常。正文真的为空时返回 (0, "doc")。"""
    try:
        text = clean(main_text(data))
        return sum(1 for ch in text if not ch.isspace()), "doc"
    except DocParseError as exc:
        logger.warning("doc binary parse failed, estimating from size: %s", exc)
    except Exception:                                        # noqa: BLE001  报价不能因为解析器的 bug 失败
        logger.exception("doc binary parse crashed, estimating from size")
    return estimate(data), "estimate"
