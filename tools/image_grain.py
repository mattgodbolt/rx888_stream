#!/usr/bin/env python3
"""Estimate grain/noise in a decoded frame as the std of flat patches.

Samples several patches that are usually flat (the black border / corners)
and reports the median per-patch luminance std. Lower = less grain. Useful
to compare captures/decode settings objectively. Multiple images can be
passed to compare side by side.

Usage:
    tools/image_grain.py frame.png
    tools/image_grain.py a.png b.png c.png
"""
import argparse
import numpy as np
from PIL import Image


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", help="decoded PNG/JPG frame(s)")
    ap.add_argument("--patch", type=int, default=40, help="patch size in px (default 40)")
    args = ap.parse_args()

    p = args.patch
    for path in args.images:
        a = np.asarray(Image.open(path).convert("L")).astype(np.float32)
        h, w = a.shape
        # corner/edge patches that are usually in the blanking border
        patches = [
            a[20:20 + p, 20:20 + p],
            a[20:20 + p, w - 20 - p:w - 20],
            a[h - 20 - p:h - 20, 20:20 + p],
            a[h - 20 - p:h - 20, w - 20 - p:w - 20],
        ]
        s = float(np.median([q.std() for q in patches]))
        print(f"{path}: grain(border-patch median std)={s:.2f}  ({w}x{h})")


if __name__ == "__main__":
    main()
