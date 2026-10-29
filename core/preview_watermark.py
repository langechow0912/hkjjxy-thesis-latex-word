"""Brand only the Haikou local formatter's preview; keep its source intact."""
from pathlib import Path


def haikou_preview(source, school_id):
    if school_id != "hkjjxy":
        return source

    import fitz

    source_path = Path(source)
    target = source_path.with_name(source_path.stem + "-preview.pdf")
    with fitz.open(source_path) as document:
        for page in document:
            # Built-in CJK font: no system font installation or network download.
            bounds = page.rect * page.derotation_matrix
            size = min(36, bounds.width / 7)
            for fraction in (0.28, 0.55, 0.82):
                origin = fitz.Point(bounds.x0 + bounds.width * 0.25,
                                    bounds.y0 + bounds.height * fraction)
                page.insert_text(origin, "良品论文", fontname="china-s",
                                 fontsize=size, color=(0.55, 0.55, 0.55),
                                 fill_opacity=0.18,
                                 morph=(origin, fitz.Matrix(30)), overlay=True)
        document.save(target, garbage=4, deflate=True)
    return str(target)
