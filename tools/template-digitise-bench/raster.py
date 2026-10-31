"""PDF -> page images, the 2026-10-16 spike's settings (spike raster.py).

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/template-digitise-bench/raster.py \
        --pdf <dir>/bicl.pdf --out <dir>

Per page i: <stem>_p<i>_clean.png (150 dpi) and <stem>_p<i>_photo.jpg (rotated 2.5 deg,
expand, fill (200, 195, 185), bicubic; GaussianBlur 0.9; lighting gradient 0.78-1.05
across x 0.92-1.0 down; JPEG quality 70). pymupdf, Pillow and numpy live in benchenv
only and are imported inside `render`, so tests can import this module without them.
"""
from __future__ import annotations

import argparse
import os
import sys

DPI = 150
ROTATE_DEG = 2.5
FILL = (200, 195, 185)
BLUR = 0.9
GRAD_X = (0.78, 1.05)
GRAD_Y = (0.92, 1.0)
JPEG_QUALITY = 70


def page_names(stem, n):
    return [(f"{stem}_p{i}_clean.png", f"{stem}_p{i}_photo.jpg") for i in range(1, n + 1)]


def photo(im):
    """The spike's 'phone photo' of one clean page (a PIL RGB image)."""
    import numpy as np
    from PIL import Image, ImageFilter
    im = im.rotate(ROTATE_DEG, expand=True, fillcolor=FILL, resample=Image.BICUBIC)
    im = im.filter(ImageFilter.GaussianBlur(BLUR))
    a = np.asarray(im).astype(np.float32)
    h, w, _ = a.shape
    g = np.linspace(*GRAD_X, w)[None, :, None] * np.linspace(*GRAD_Y, h)[:, None, None]
    return Image.fromarray(np.clip(a * g, 0, 255).astype(np.uint8))


def render(pdf, out):
    import pymupdf
    from PIL import Image
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pdf))[0]
    doc = pymupdf.open(pdf)
    names = page_names(stem, doc.page_count)
    for pg, (clean, ph) in zip(doc, names):
        cp = os.path.join(out, clean)
        pg.get_pixmap(dpi=DPI).save(cp)
        photo(Image.open(cp).convert("RGB")).save(os.path.join(out, ph), quality=JPEG_QUALITY)
    print(f"{stem}: {doc.page_count} pages -> {len(names) * 2} files")
    return names


def main(argv=None):
    ap = argparse.ArgumentParser(prog="raster")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    render(a.pdf, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
