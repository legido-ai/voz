#!/usr/bin/env python3
"""Voz brand mark — the single source for the logo, the bot avatar and the app icons.

The mark is the one the web app already used (app/static/icons/icon-1024.png): four
rounded waveform bars in two oranges, on near-black, inside a faint ring. It is
redrawn here from geometry so every size is crisp and the clear-space is
controlled: the original put the bars at 32 % of the canvas, which turns into an
orange smudge in a Telegram avatar or a 40 px chat list. Default here is 55 %.

Usage:
    python3 brand/logo.py                       # write brand/*.png at 1024
    python3 brand/logo.py --icons app/static/icons   # also emit the PWA icon set
    python3 brand/logo.py --size 256 --out /tmp/avatar.png
"""
from __future__ import annotations

import argparse
import pathlib

from PIL import Image, ImageDraw, ImageFont

BG = (8, 9, 12, 255)          # --bg      site background
ACCENT = (255, 138, 61, 255)  # --accent  #ff8a3d
SOFT = (255, 179, 122, 255)   # --accent-soft  #ffb37a
RING = (46, 36, 30, 255)
INK = (242, 242, 244, 255)    # --ink
SS = 4                        # supersampling factor

#: (height relative to the tallest bar, colour) — measured off the original icon
BARS = [(0.385, SOFT), (0.665, ACCENT), (1.000, ACCENT), (0.590, SOFT)]
GAP_RATIO = 60 / 96           # gap between bars, as a fraction of bar width

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _bars(size: int, span_frac: float, tallest_frac: float) -> Image.Image:
    W = size * SS
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    span = W * span_frac
    bar_w = span / (3 * (1 + GAP_RATIO) + 1)
    pitch = bar_w * (1 + GAP_RATIO)
    x = (W - span) / 2 + bar_w / 2
    tallest = W * tallest_frac
    for i, (rel, colour) in enumerate(BARS):
        h = tallest * rel
        draw.rounded_rectangle([x + i * pitch - bar_w / 2, W / 2 - h / 2,
                                x + i * pitch + bar_w / 2, W / 2 + h / 2],
                               radius=bar_w / 2, fill=colour)
    return layer.resize((size, size), Image.LANCZOS)


def _ring(size: int, radius_frac: float = 0.39, width: int = 10) -> Image.Image:
    W = size * SS
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    r = W * radius_frac
    draw.ellipse([W / 2 - r, W / 2 - r, W / 2 + r, W / 2 + r], outline=RING, width=width * SS)
    return layer.resize((size, size), Image.LANCZOS)


def _glow(size: int, strength: int = 150) -> Image.Image:
    from PIL import ImageFilter
    W = size * SS
    mask = Image.new("L", (W, W), 0)
    draw = ImageDraw.Draw(mask)
    base = W * 0.34
    for i in range(28):
        r = base * (1 + i * 0.045)
        draw.ellipse([W / 2 - r, W / 2 - r, W / 2 + r, W / 2 + r],
                     fill=int(strength * (1 - i / 28) ** 2.2))
    mask = mask.filter(ImageFilter.GaussianBlur(W * 0.03)).resize((size, size), Image.LANCZOS)
    layer = Image.new("RGBA", (size, size), ACCENT)
    layer.putalpha(mask)
    return layer


def mark(size: int = 1024, ring: bool = True, glow: bool = False,
         span_frac: float = 0.55, tallest_frac: float = 0.605) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    if glow:
        img = Image.alpha_composite(img, _glow(size))
    if ring:
        img = Image.alpha_composite(img, _ring(size))
    return Image.alpha_composite(img, _bars(size, span_frac, tallest_frac))


def lockup(height: int = 768, word: str = "voz") -> Image.Image:
    """Horizontal lockup: mark + wordmark, transparent background."""
    mark_size = int(height * 0.73)
    badge = Image.new("RGBA", (mark_size, mark_size), (0, 0, 0, 0))
    badge = Image.alpha_composite(badge, _bars(mark_size, span_frac=0.72, tallest_frac=0.84))
    font = ImageFont.truetype(FONT_BOLD, int(mark_size * 0.59))
    bbox = font.getbbox(word)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    width = mark_size + int(mark_size * 0.13) + text_w + int(mark_size * 0.06)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(badge, (0, (height - mark_size) // 2))
    ImageDraw.Draw(canvas).text(
        (mark_size + int(mark_size * 0.13) - bbox[0], (height - text_h) / 2 - bbox[1]),
        word, font=font, fill=INK)
    return canvas


def icon_set(out: pathlib.Path) -> None:
    """PWA icons: a squircle, so iOS/Android mask against the site background."""
    out.mkdir(parents=True, exist_ok=True)
    for side in (192, 512, 1024):
        mask = Image.new("L", (side * SS, side * SS), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, side * SS - 1, side * SS - 1], radius=int(side * SS * 0.204), fill=255)
        mask = mask.resize((side, side), Image.LANCZOS)
        img = mark(side)
        img.putalpha(mask)
        img.save(out / f"icon-{side}.png")
    mark(180).save(out / "apple-touch-icon.png")


def main() -> None:
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--out", type=pathlib.Path, default=here)
    ap.add_argument("--icons", type=pathlib.Path, help="also write the PWA icon set here")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    m = mark(args.size)
    m.save(args.out / f"voz-mark-{args.size}.png")
    m.convert("RGB").resize((256, 256), Image.LANCZOS).save(args.out / "voz-avatar-256.jpg", quality=94)
    lockup().save(args.out / "voz-lockup.png")
    mark(args.size, glow=True, ring=False, span_frac=0.58, tallest_frac=0.64).save(args.out / "voz-mark-glow.png")
    if args.icons:
        icon_set(args.icons)
        print(f"icons written to {args.icons}")
    print(f"brand assets written to {args.out}")


if __name__ == "__main__":
    main()
