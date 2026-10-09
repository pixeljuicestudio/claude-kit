#!/usr/bin/env python3
"""
Draws kit.ico — an icon for a desktop shortcut to Kit.

    python icon.py           (needs Pillow: pip install pillow)

The only file in Kit that is not stdlib-only, and optional: kit.ico is
already committed. A .bat file cannot carry a custom icon; Windows always
draws the generic gears. So a desktop item is a .lnk shortcut pointing at
SKILLS.bat, and a .lnk CAN carry one. This makes the file it points at.

Generated rather than downloaded so it can be regenerated after a palette
change, and so there is no binary in the repo whose origin nobody remembers.
Colours are lifted from the page's own palette in build.py, which is the whole
point of it being code: change --live there and re-run this, and the icon
follows.

Drawn at every size Windows asks for rather than scaling one master. Explorer
picks 16px for the taskbar and 256px for large icons, and a 256px "K"
downscaled to 16 turns to mush - the small sizes need thicker strokes and less
padding, which is a different drawing, not a resample.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                       # pragma: no cover - optional dependency
    Image = ImageDraw = ImageFont = None

HERE = Path(__file__).resolve().parent
OUT = HERE / "kit.ico"

# Straight from build.py's :root block.
BG = (20, 23, 28, 255)        # --bg    #14171C
EDGE = (58, 66, 78, 255)      # --edge2 #3A424E
LIVE = (95, 191, 135, 255)    # --live  #5FBF87

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# Bold faces only. A regular weight vanishes at 16px against a dark tile.
# Tried in order; whichever exists on this OS wins, else Pillow's default.
FONTS = {
    "win32": ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "C:/Windows/Fonts/calibrib.ttf"],
    "darwin": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
               "/Library/Fonts/Arial Bold.ttf",
               "/System/Library/Fonts/Helvetica.ttc"],
}.get(sys.platform, [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
])


def _font(px: int):
    for path in FONTS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def tile(size: int) -> Image.Image:
    """One square. Draws at 4x and downsamples so the corners are not jagged."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Small icons get a tighter radius and no inset: at 16px, rounding and
    # padding eat the same handful of pixels the letter needs.
    inset = 0 if size <= 24 else s // 22
    radius = s // 5 if size <= 24 else s // 4.5

    d.rounded_rectangle([inset, inset, s - 1 - inset, s - 1 - inset],
                        radius=radius, fill=BG,
                        outline=EDGE, width=max(1, s // 64))

    # Fit the K by measuring, not by guessing a point size. Different Windows
    # builds ship different faces, and a hardcoded size that fits Segoe would
    # overflow Arial.
    target = s * (0.66 if size <= 24 else 0.58)
    px, f = int(s * 0.7), None
    for _ in range(40):
        f = _font(px)
        box = d.textbbox((0, 0), "K", font=f)
        w, h = box[2] - box[0], box[3] - box[1]
        if max(w, h) <= target or px <= 6:
            break
        px = int(px * 0.92)

    box = d.textbbox((0, 0), "K", font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1]), "K", font=f, fill=LIVE)

    return img.resize((size, size), Image.LANCZOS)


def build() -> Path:
    """Assemble the .ico container by hand.

    Pillow's ICO writer will NOT do this. Passing sizes= plus append_images
    looks like it works - it returns cleanly and writes a file - but it
    intersects the requested sizes with the base image's own size, so handing
    it a 16px first frame silently produced a one-entry, 16-only icon. It was
    589 bytes for what should have been nine frames including a 256; caught by
    reading the directory back rather than trusting the write.

    Entries are PNG-compressed, which Windows has accepted at every size since
    Vista and which avoids hand-rolling the BMP AND-mask.
    """
    frames = [(n, tile(n)) for n in SIZES]

    blobs = []
    for _, img in frames:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        blobs.append(buf.getvalue())

    # ICONDIR, then one 16-byte ICONDIRENTRY per frame, then the payloads.
    offset = 6 + 16 * len(frames)
    header = struct.pack("<HHH", 0, 1, len(frames))
    entries, payload = b"", b""
    for (n, _), blob in zip(frames, blobs):
        # 256 is stored as 0 - the field is one byte.
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0,
                               1, 32, len(blob), offset)
        payload += blob
        offset += len(blob)

    OUT.write_bytes(header + entries + payload)
    verify()
    return OUT


def verify() -> None:
    """Read the directory back. A write that took is not the same as a write.

    This is the same rule serve.py applies to every toggle, and it exists
    here because the first
    version of this file failed exactly this check.
    """
    b = OUT.read_bytes()
    count = struct.unpack("<H", b[4:6])[0]
    got = sorted((b[6 + i * 16] or 256) for i in range(count))
    want = sorted(SIZES)
    if got != want:
        raise SystemExit("icon write did not take: wanted %s, file has %s"
                         % (want, got))


if __name__ == "__main__":
    if Image is None:
        raise SystemExit("icon.py needs Pillow: pip install pillow "
                         "(kit.ico is already committed - you only need this to redraw it)")
    p = build()
    print("wrote %s (%d bytes, %d sizes)" % (p, p.stat().st_size, len(SIZES)))
