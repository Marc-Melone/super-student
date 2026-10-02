"""Draws the Super Student icon (a page with a highlighted line) as icon.png and icon.icns."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

INK = (30, 42, 74, 255)
PAGE = (255, 255, 255, 255)
LINE = (195, 202, 216, 255)
HIGHLIGHT = (255, 226, 82, 255)


def draw(size: int = 1024) -> Image.Image:
    s = size / 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # macOS icon grid: 824px rounded square centered in 1024, with a soft shadow
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([100 * s, 112 * s, 924 * s, 936 * s], radius=185 * s, fill=(0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18 * s)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([100 * s, 100 * s, 924 * s, 924 * s], radius=185 * s, fill=INK)
    # the page, slightly tilted feel via offset back sheet
    d.rounded_rectangle([318 * s, 222 * s, 742 * s, 790 * s], radius=34 * s, fill=(70, 86, 128, 255))
    d.rounded_rectangle([282 * s, 196 * s, 706 * s, 764 * s], radius=34 * s, fill=PAGE)
    bar = lambda x0, y, x1, color: d.rounded_rectangle([x0 * s, y * s, x1 * s, (y + 30) * s], radius=15 * s, fill=color)
    bar(346, 300, 642, LINE)
    d.rounded_rectangle([322 * s, 380 * s, 668 * s, 470 * s], radius=22 * s, fill=HIGHLIGHT)
    bar(346, 410, 642, INK)
    bar(346, 520, 642, LINE)
    bar(346, 620, 540, LINE)
    return img


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    big = draw(1024)
    big.save(out / "icon.png")
    big.save(out / "icon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
