#!/usr/bin/env python3
"""ai-excited-01: one image to excite the AI judge.

A GIANT paper plane climbs diagonally over a neon city at dusk,
trailing a rainbow smoke ribbon, while one tiny person on a rooftop
reaches up at it. Big clean "WOW!" in the corner.

The program draws the whole poster itself (own rasterizer + own PNG
encoder, standard library only), writes exactly one file,
``entry.png`` (1024x1024, <= 1 MiB), into the directory named by the
``MAKEFUN_OUTPUT_DIR`` environment variable (default ``/output``).
Every number is fixed: no clock, no randomness without a fixed seed,
no network, no files read. Two runs produce byte-identical output.
"""
import math
import os
import struct
import sys
import zlib

W = 1024
H = 1024


def clamp(v, lo=0, hi=255):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return (int(lerp(c1[0], c2[0], t)), int(lerp(c1[1], c2[1], t)),
            int(lerp(c1[2], c2[2], t)))


def lcg(seed):
    """Deterministic pseudo-random stream from a fixed seed."""
    s = seed & 0xFFFFFFFF

    def nxt():
        nonlocal s
        s = (1664525 * s + 1013904223) & 0xFFFFFFFF
        return s / 4294967296.0
    return nxt


class Canvas:
    """A plain RGB pixel buffer with a few drawing helpers."""

    def __init__(self, w=W, h=H):
        self.w = w
        self.h = h
        self.px = bytearray(w * h * 3)

    def set(self, x, y, col, a=1.0):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            if a >= 1.0:
                self.px[i] = col[0]
                self.px[i + 1] = col[1]
                self.px[i + 2] = col[2]
            else:
                self.px[i] = clamp(int(self.px[i] * (1 - a) + col[0] * a))
                self.px[i + 1] = clamp(int(self.px[i + 1] * (1 - a)
                                           + col[1] * a))
                self.px[i + 2] = clamp(int(self.px[i + 2] * (1 - a)
                                           + col[2] * a))

    def vgrad(self, stops):
        """Vertical gradient; stops is a list of (y_frac, colour)."""
        for y in range(self.h):
            t = y / (self.h - 1)
            c = stops[-1][1]
            for k in range(len(stops) - 1):
                y0, c0 = stops[k]
                y1, c1 = stops[k + 1]
                if y0 <= t <= y1:
                    c = mix(c0, c1, 0.0 if y1 == y0 else (t - y0) / (y1 - y0))
                    break
            base = y * self.w * 3
            for x in range(self.w):
                i = base + x * 3
                self.px[i] = c[0]
                self.px[i + 1] = c[1]
                self.px[i + 2] = c[2]

    def disc(self, cx, cy, r, col, a=1.0):
        r2 = r * r
        x0 = max(0, int(cx - r))
        x1 = min(self.w - 1, int(cx + r) + 1)
        y0 = max(0, int(cy - r))
        y1 = min(self.h - 1, int(cy + r) + 1)
        for y in range(y0, y1 + 1):
            dy = y - cy
            for x in range(x0, x1 + 1):
                dx = x - cx
                if dx * dx + dy * dy <= r2:
                    self.set(x, y, col, a)

    def glow(self, cx, cy, r, col, peak=0.55):
        """Soft radial glow: alpha falls off to zero at radius r."""
        x0 = max(0, int(cx - r))
        x1 = min(self.w - 1, int(cx + r) + 1)
        y0 = max(0, int(cy - r))
        y1 = min(self.h - 1, int(cy + r) + 1)
        for y in range(y0, y1 + 1):
            dy = (y - cy) / r
            for x in range(x0, x1 + 1):
                dx = (x - cx) / r
                d2 = dx * dx + dy * dy
                if d2 < 1.0:
                    self.set(x, y, col, peak * (1.0 - d2) * (1.0 - d2))

    def poly(self, pts, col, a=1.0):
        """Scanline polygon fill (even-odd rule)."""
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        y0 = max(0, int(min(ys)))
        y1 = min(self.h - 1, int(max(ys)) + 1)
        n = len(pts)
        for y in range(y0, y1 + 1):
            xs = []
            for k in range(n):
                x1, y1p = pts[k]
                x2, y2p = pts[(k + 1) % n]
                if (y1p <= y < y2p) or (y2p <= y < y1p):
                    t = (y - y1p) / (y2p - y1p)
                    xs.append(x1 + t * (x2 - x1))
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                xa = max(0, int(math.ceil(xs[k])))
                xb = min(self.w - 1, int(math.floor(xs[k + 1])))
                for x in range(xa, xb + 1):
                    self.set(x, y, col, a)

    def line(self, x0, y0, x1, y1, width, col, a=1.0):
        """Thick line as a filled quad plus round caps."""
        dx = x1 - x0
        dy = y1 - y0
        ln = math.hypot(dx, dy)
        if ln == 0:
            self.disc(x0, y0, width / 2, col, a)
            return
        nx = -dy / ln * width / 2
        ny = dx / ln * width / 2
        self.poly([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny),
                   (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], col, a)
        self.disc(x0, y0, width / 2, col, a)
        self.disc(x1, y1, width / 2, col, a)

    def save_png(self, path):
        """Hand-rolled PNG: signature, IHDR, zlib IDAT, IEND, CRC32s."""
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)  # filter: None, one byte per scanline
            raw += self.px[y * self.w * 3:(y + 1) * self.w * 3]
        comp = zlib.compress(bytes(raw), 9)

        def chunk(typ, data):
            out = struct.pack(">I", len(data)) + typ + data
            out += struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
            return out

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(png)
        return len(png)


# --- bitmap font: 7 wide x 9 tall, letters defined here by hand ---
FONT = {
    "W": [
        "##...##",
        "##...##",
        "##...##",
        "##...##",
        "##...##",
        "###.###",
        "###.###",
        ".##.##.",
        "..#.#..",
    ],
    "O": [
        ".#####.",
        "#.....#",
        "#.....#",
        "#.....#",
        "#.....#",
        "#.....#",
        "#.....#",
        "#.....#",
        ".#####.",
    ],
    "!": [
        "...#...",
        "...#...",
        "...#...",
        "...#...",
        "...#...",
        "...#...",
        ".......",
        "...#...",
        "...#...",
    ],
}


def text_width(s, scale):
    return len(s) * (7 * scale + scale) - scale


def draw_text(cv, s, x, y, scale, col, outline=None):
    """Draw text at top-left (x, y); outline is a thick shadow pass."""
    cw = 7 * scale
    step = cw + scale
    if outline is not None:
        for ox, oy in ((-scale, 0), (scale, 0), (0, -scale), (0, scale),
                       (-scale, -scale), (scale, -scale),
                       (-scale, scale), (scale, scale)):
            draw_text(cv, s, x + ox, y + oy, scale, outline, None)
    for ci, ch in enumerate(s):
        glyph = FONT.get(ch)
        if glyph is None:
            continue
        gx = x + ci * step
        for ry, row in enumerate(glyph):
            for rx, bit in enumerate(row):
                if bit == "#":
                    for dy in range(scale):
                        for dx in range(scale):
                            cv.set(gx + rx * scale + dx,
                                   y + ry * scale + dy, col)


def bez(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0]
            + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1]
            + t ** 3 * p3[1])


def draw_poster():
    cv = Canvas()

    # --- sky: deep indigo -> violet -> hot magenta -> amber horizon ---
    cv.vgrad([(0.0, (16, 10, 54)), (0.42, (64, 24, 110)),
              (0.62, (150, 40, 120)), (0.78, (255, 110, 60)),
              (1.0, (255, 170, 80))])

    # --- stars in the upper sky ---
    rnd = lcg(20261116)
    for _ in range(90):
        x = rnd() * 1024
        y = rnd() * 380
        r = 1 + rnd() * 1.6
        cv.disc(x, y, r, (255, 240, 200), 0.35 + rnd() * 0.5)

    # --- low sun, high enough to clear the rooftops, with a halo ---
    cv.glow(795, 520, 260, (255, 190, 90), 0.55)
    cv.glow(795, 520, 150, (255, 220, 140), 0.7)
    cv.disc(795, 520, 66, (255, 236, 170))
    cv.disc(795, 520, 52, (255, 248, 210))

    # --- far skyline (purple haze) ---
    rnd2 = lcg(777)
    x = -20
    while x < 1044:
        wdt = 60 + rnd2() * 90
        hgt = 120 + rnd2() * 170
        top = 1024 - 260 - hgt
        cv.poly([(x, 1024), (x, top), (x + wdt, top), (x + wdt, 1024)],
                (88, 44, 130), 0.85)
        x += wdt + 8

    # --- near skyline (dark navy) with lit windows; keep the sun's
    #     patch of sky clear by capping building height near x 650-950 ---
    rnd3 = lcg(4242)
    buildings = []
    x = -30
    while x < 1054:
        wdt = 90 + rnd3() * 110
        hgt = 200 + rnd3() * 260
        if 620 < x + wdt / 2 < 960:
            hgt = min(hgt, 300)
        top = 1024 - hgt
        buildings.append((x, top, wdt))
        cv.poly([(x, 1024), (x, top), (x + wdt, top), (x + wdt, 1024)],
                (18, 14, 48))
        if rnd3() < 0.4:
            cv.line(x + wdt / 2, top, x + wdt / 2, top - 34, 4,
                    (18, 14, 48))
            cv.disc(x + wdt / 2, top - 38, 4, (255, 80, 90), 0.9)
        for wy in range(int(top) + 22, 1000, 34):
            for wx in range(int(x) + 14, int(x + wdt) - 14, 30):
                if rnd3() < 0.42:
                    col = (255, 200, 90) if rnd3() < 0.7 else (120, 230, 255)
                    cv.poly([(wx, wy), (wx + 14, wy), (wx + 14, wy + 18),
                             (wx, wy + 18)], col, 0.9)
        x += wdt + 14

    # --- rainbow smoke trail: thick bezier ribbon, colour bands ---
    RAINBOW = [(255, 60, 60), (255, 150, 40), (255, 220, 60),
               (80, 220, 110), (60, 170, 255), (170, 90, 255)]
    P0 = (60, 880)
    P1 = (300, 700)
    P2 = (420, 950)
    P3 = (575, 635)  # ends under the wing tip, so the trail emerges
    # from behind the plane instead of floating as a separate blob
    N = 130
    prev = None
    for k in range(N + 1):
        t = k / N
        px, py = bez(P0, P1, P2, P3, t)
        band = RAINBOW[min(5, int(t * 6))]
        wdt = 26 + 34 * t
        if prev is not None:
            cv.line(prev[0], prev[1], px, py, wdt, band, 0.92)
            cv.line(prev[0], prev[1], px, py, wdt * 0.45,
                    (255, 255, 255), 0.35)
        prev = (px, py)
    for k in range(0, N + 1, 9):
        t = k / N
        px, py = bez(P0, P1, P2, P3, t)
        cv.glow(px, py, 34 + 30 * t, RAINBOW[min(5, int(t * 6))], 0.16)

    # --- speed lines parallel to the climb ---
    for (sx, sy, ln) in [(150, 430, 150), (260, 350, 190), (90, 560, 120),
                         (420, 300, 170), (520, 210, 130), (330, 480, 110)]:
        cv.line(sx, sy, sx + ln, sy - ln * 0.55, 5, (255, 255, 255), 0.55)

    # --- birds ---
    for (bx, by, s) in [(830, 180, 16), (880, 230, 12), (790, 260, 10),
                        (920, 150, 9), (700, 130, 11)]:
        cv.line(bx - s, by, bx, by - s * 0.55, 4, (30, 16, 60))
        cv.line(bx, by - s * 0.55, bx + s, by, 4, (30, 16, 60))

    # --- a small distant paper plane, for scale ---
    cv.poly([(196, 316), (150, 336), (186, 342)], (240, 240, 255))
    cv.poly([(196, 316), (186, 342), (176, 352)], (190, 210, 245))

    # --- THE GIANT PAPER PLANE (nose upper-right, climbing) ---
    NOSE = (880, 300)
    TL = (430, 470)
    TR = (560, 640)
    KEEL = (640, 470)
    cv.glow(650, 460, 330, (255, 255, 255), 0.10)
    cv.poly([NOSE, TL, KEEL], (255, 255, 255))          # upper wing
    cv.poly([NOSE, KEEL, TR], (196, 222, 255))          # lower wing
    cv.poly([NOSE, KEEL, (700, 430)], (168, 196, 235))  # centre fold
    cv.line(NOSE[0], NOSE[1], KEEL[0], KEEL[1], 5, (120, 150, 205), 0.9)
    cv.line(TL[0], TL[1], KEEL[0], KEEL[1], 4, (150, 175, 220), 0.8)
    cv.line(TR[0], TR[1], KEEL[0], KEEL[1], 4, (150, 175, 220), 0.8)
    cv.line(NOSE[0], NOSE[1], TL[0], TL[1], 6, (255, 255, 255), 0.95)
    # red racing stripes near the nose
    cv.line(800, 348, 838, 322, 10, (255, 70, 80), 0.95)
    cv.line(770, 368, 808, 342, 10, (255, 70, 80), 0.95)

    # --- tiny figure on a rooftop, reaching up at the plane ---
    # Stand on the roof of the building whose span holds x = 838.
    roof_y = 700
    for (bx, btop, bwdt) in buildings:
        if bx <= 838 < bx + bwdt:
            roof_y = btop
            break
    fx, fy = 838, roof_y
    cv.line(fx, fy, fx + 2, fy - 26, 7, (12, 8, 30))
    cv.disc(fx + 1, fy - 34, 7, (12, 8, 30))
    cv.line(fx + 1, fy - 24, fx - 12, fy - 8, 5, (12, 8, 30))
    cv.line(fx + 1, fy - 24, fx + 13, fy - 6, 5, (12, 8, 30))
    cv.line(fx + 1, fy - 22, fx + 20, fy - 46, 5, (12, 8, 30))
    cv.glow(fx + 22, fy - 50, 16, (255, 220, 120), 0.8)
    cv.disc(fx + 22, fy - 50, 4, (255, 240, 160))

    # --- "WOW!" big and clean, top-left, yellow with a dark outline ---
    scale = 13
    tw = text_width("WOW!", scale)
    draw_text(cv, "WOW!", 48, 52, scale, (255, 214, 64),
              outline=(24, 10, 60))
    cv.line(52, 52 + 9 * scale + 18, 52 + tw * 0.72,
            52 + 9 * scale + 6, 7, (255, 214, 64), 0.95)
    return cv


def main():
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "entry.png")
    size = draw_poster().save_png(path)
    print("wrote %s (%d bytes)" % (path, size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
