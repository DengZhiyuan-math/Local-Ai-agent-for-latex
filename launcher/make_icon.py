#!/usr/bin/env python3
"""Draw launcher/prism.ico (a prism splitting a beam of light), standard library only.

    python launcher/make_icon.py

The .ico holds PNG images at 16, 24, 32, 48, 64 and 256 pixels.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "prism.ico"
SIZES = (16, 24, 32, 48, 64, 256)
SS = 4                                    # supersampling per axis

BG = (0x1F, 0x2A, 0x37)
RAINBOW = [(0xE5, 0x48, 0x4D), (0xF5, 0x9E, 0x0B), (0xFA, 0xCC, 0x15),
           (0x22, 0xC5, 0x5E), (0x3B, 0x82, 0xF6), (0x8B, 0x5C, 0xF6)]
TRI = [(0.50, 0.17), (0.18, 0.80), (0.82, 0.80)]
EXIT = (0.60, 0.53)


def in_poly(x, y, pts):
    inside, j = False, len(pts) - 1
    for i, (xi, yi) in enumerate(pts):
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def seg_dist(x, y, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


def in_round_rect(x, y, r=0.2):
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    return 0 <= x <= 1 and 0 <= y <= 1 and math.hypot(x - cx, y - cy) <= r


def sample(x, y):
    """RGBA of the icon at unit coordinates (x, y)."""
    if not in_round_rect(x, y):
        return (0, 0, 0, 0)
    col = BG
    if seg_dist(x, y, (0.02, 0.62), EXIT) < 0.035:                  # incoming white beam
        col = (0xF2, 0xF2, 0xF2)
    for i, c in enumerate(RAINBOW):                                 # outgoing spectrum
        y0, y1 = 0.36 + 0.07 * i, 0.36 + 0.07 * (i + 1)
        if in_poly(x, y, [EXIT, (1.0, y0), (1.0, y1)]):
            col = c
    if in_poly(x, y, TRI):                                          # glass body
        col = tuple(int(0.72 * v + 0.28 * 255) for v in col)
    if min(seg_dist(x, y, TRI[i], TRI[(i + 1) % 3]) for i in range(3)) < 0.03:
        col = (0xFF, 0xFF, 0xFF)
    return (*col, 255)


def render(size):
    rows = []
    for py in range(size):
        row = bytearray([0])                                        # PNG filter: none
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    r, g, b, a = sample((px + (sx + 0.5) / SS) / size,
                                        (py + (sy + 0.5) / SS) / size)
                    acc[0] += r * a; acc[1] += g * a; acc[2] += b * a; acc[3] += a
            a = acc[3]
            row += bytes([acc[0] // a, acc[1] // a, acc[2] // a, a // (SS * SS)] if a else [0, 0, 0, 0])
        rows.append(bytes(row))
    return png(size, b"".join(rows))


def png(size, raw):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    images = [render(s) for s in SIZES]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = len(head) + 16 * len(images)
    entries, data = b"", b""
    for s, img in zip(SIZES, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset + len(data))
        data += img
    OUT.write_bytes(head + entries + data)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
