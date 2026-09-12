"""Regenerate the site logo asset from the lab's master logo.

The master is a 1024x1024 RGB PNG at 868 KB with an opaque cream ground. Both
of those are wrong for the web build: the weight matters for something used as
a page background, and an opaque ground would paint a cream square behind the
watermark instead of letting the page show through.

This produces a 256x256 RGBA PNG of about 97 KB, used for both the header mark
and the watermark. Run it only when the master logo changes:

    python -m scripts.make_logo_asset

Written against zlib and struct rather than Pillow, so the project keeps its
short dependency list and this stays runnable from a clean checkout.
"""

from __future__ import annotations

import pathlib
import struct
import sys
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.http_client import BROWSER_USER_AGENT

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "assets" / "yabilab-logo.png"
SOURCE_URL = (
    "https://raw.githubusercontent.com/wicebing/Copyright/main/yabilab_logo_20241105.png"
)
SCALE = 4  # 1024 -> 256


def decode(data: bytes) -> tuple[int, int, bytearray]:
    """Decode a non-interlaced 8-bit RGB or RGBA PNG into flat RGB bytes."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"

    pos = 8
    idat = bytearray()
    width = height = channels = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and colour in (2, 6), f"unsupported depth/colour {depth}/{colour}"
            assert body[12] == 0, "interlaced PNG not supported"
            channels = 3 if colour == 2 else 4
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 3)
    previous = bytearray(stride)
    offset = 0

    for row in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride

        # Undo the per-scanline filter: a is the byte to the left, b above,
        # c above-left.
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = previous[i]
            c = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        previous = line

        for x in range(width):
            src = x * channels
            dst = (row * width + x) * 3
            out[dst:dst + 3] = line[src:src + 3]

    return width, height, out


def downscale(width: int, height: int, pixels: bytearray, factor: int):
    """Box-average by an integer factor, which keeps the fine linework smooth."""
    new_w, new_h = width // factor, height // factor
    out = bytearray(new_w * new_h * 3)
    area = factor * factor
    for y in range(new_h):
        for x in range(new_w):
            r = g = b = 0
            for dy in range(factor):
                base = ((y * factor + dy) * width + x * factor) * 3
                for dx in range(factor):
                    i = base + dx * 3
                    r += pixels[i]
                    g += pixels[i + 1]
                    b += pixels[i + 2]
            o = (y * new_w + x) * 3
            out[o], out[o + 1], out[o + 2] = r // area, g // area, b // area
    return new_w, new_h, out


def add_alpha(width: int, height: int, pixels: bytearray, ground: tuple[int, int, int]):
    """Make the cream ground transparent, with soft edges.

    Alpha rises with distance from the ground colour, so antialiased edge
    pixels keep partial alpha rather than being cut to a hard, jagged border.
    """
    out = bytearray(width * height * 4)
    gr, gg, gb = ground
    for i in range(width * height):
        r, g, b = pixels[i * 3], pixels[i * 3 + 1], pixels[i * 3 + 2]
        distance = max(abs(r - gr), abs(g - gg), abs(b - gb))
        alpha = 0 if distance <= 6 else min(255, int((distance - 6) * 4.5))
        o = i * 4
        out[o], out[o + 1], out[o + 2], out[o + 3] = r, g, b, alpha
    return out


def encode(width: int, height: int, rgba: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    # Fetched with requests directly rather than through PoliteSession: that
    # returns decoded text, and a PNG needs the raw bytes.
    import requests

    response = requests.get(
        SOURCE_URL, timeout=60, headers={"User-Agent": BROWSER_USER_AGENT}
    )
    if response.status_code >= 400:
        print(f"could not fetch the logo: HTTP {response.status_code}")
        return 1

    data = response.content
    width, height, pixels = decode(data)
    print(f"source {width}x{height}, {len(data) // 1024} KB")

    # Sample a corner rather than assuming a fixed cream value.
    ground = (pixels[0], pixels[1], pixels[2])
    width, height, pixels = downscale(width, height, pixels, SCALE)
    rgba = add_alpha(width, height, pixels, ground)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(encode(width, height, rgba))
    print(f"wrote {OUT.relative_to(ROOT)}: {width}x{height}, {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
