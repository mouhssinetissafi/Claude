"""Diagnostic only: write N synthetic JPEG photos (gradient + one detailed patch)."""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(sys.argv[1])
count = int(sys.argv[2]) if len(sys.argv) > 2 else 9
out.mkdir(parents=True, exist_ok=True)
for i in range(count):
    w, h = 1600, 1200
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(0, h):
        for x in range(0, w, 4):
            c = (int(40 + 120 * x / w), int(60 + 100 * y / h), 90 + 10 * i)
            for dx in range(4):
                px[x + dx, y] = c
    draw = ImageDraw.Draw(im)
    cx, cy = int(w * ((i % 3) + 1) / 4), int(h * ((i // 3) + 1) / 4)
    for y in range(cy - 60, cy + 60, 6):
        for x in range(cx - 60, cx + 60, 6):
            draw.rectangle([x, y, x + 5, y + 5], fill=(250, 250, 250) if ((x // 6) + (y // 6)) % 2 else (5, 5, 5))
    im.save(out / f"photo_{i + 1:02d}.jpg", quality=90)
print(f"wrote {count} photos to {out}")
