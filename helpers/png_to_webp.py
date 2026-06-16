#!/usr/bin/env python3
import sys
from pathlib import Path
from PIL import Image


def convert_png_to_webp(input_path: str, output_path: str = None, quality: int = 90):
    src = Path(input_path)
    dst = Path(output_path) if output_path else src.with_suffix(".webp")

    img = Image.open(src)

    # Preserve RGBA (transparency) if present, else convert to RGBA to be safe
    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")

    img.save(dst, format="WEBP", quality=quality, lossless=False)
    print(f"Saved: {dst}  (mode={img.mode}, size={img.size})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default: convert the single PNG in this directory
        convert_png_to_webp("AICRAFT_logo_reversed.png")
    elif len(sys.argv) == 2:
        convert_png_to_webp(sys.argv[1])
    else:
        convert_png_to_webp(sys.argv[1], sys.argv[2])
