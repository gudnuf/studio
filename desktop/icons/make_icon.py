#!/usr/bin/env python3
"""Studio's app icon, drawn from scratch (stdlib only), at every size macOS wants.

    python3 desktop/icons/make_icon.py        # writes the PNGs + icon.icns next to this file

The mark: a vertical 9:16 frame (the shape of the video Studio makes) with a play triangle in
it, warm amber (the UI's --accent #f0a43a / --accent-2 #ffbe5c) on a dark tile (the UI's
--bg #111113 family). Every size is rendered directly from signed distance fields, with the
strokes held to a minimum pixel width, so 16 px stays legible instead of being a blurry
downscale of 1024. icon.icns is packed with macOS's /usr/bin/iconutil.
"""
import math, os, shutil, struct, subprocess, sys, tempfile, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
U = 1024.0                       # design units


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


TILE_TOP, TILE_BOT = hexrgb("#2b2a2f"), hexrgb("#111113")
AMBER_TOP, AMBER_BOT = hexrgb("#ffc56b"), hexrgb("#ec9a2c")
RIM = hexrgb("#ffffff")


def sd_round_box(px, py, cx, cy, hw, hh, r):
    qx, qy = abs(px - cx) - hw + r, abs(py - cy) - hh + r
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sd_triangle(px, py, pts):
    """Signed distance to a triangle (negative inside)."""
    d = 1e9
    for i in range(3):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % 3]
        ex, ey = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        t = max(0.0, min(1.0, (wx * ex + wy * ey) / (ex * ex + ey * ey)))
        dx, dy = wx - ex * t, wy - ey * t
        d = min(d, dx * dx + dy * dy)
    # inside: the point is on the same side of all three edges
    (x1, y1), (x2, y2), (x3, y3) = pts
    def side(ax, ay, bx, by):
        return (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    a, b, c = side(x1, y1, x2, y2), side(x2, y2, x3, y3), side(x3, y3, x1, y1)
    inside = (a >= 0 and b >= 0 and c >= 0) or (a <= 0 and b <= 0 and c <= 0)
    return -math.sqrt(d) if inside else math.sqrt(d)


def cover(d, aa):
    """Coverage from a signed distance (design units) with an AA band of `aa` units."""
    return max(0.0, min(1.0, 0.5 - d / aa))


def over(dst, src, a):
    return tuple(s * a + d * (1 - a) for s, d in zip(src, dst))


def render(size):
    s = U / size                         # design units per pixel
    aa = s * 1.0
    stroke = max(36.0, 1.6 * s)          # the frame's line: never thinner than ~1.6 px
    # geometry (Apple's grid: an 824-unit tile centred in 1024)
    tile = (512.0, 512.0, 412.0, 412.0, 186.0)
    fw, fh = 292.0, 292.0 * 16 / 9       # the 9:16 frame, outer size
    fx, fy = 512.0, 512.0
    fr = 58.0
    tri_h = 176.0
    tri = [(fx - tri_h * 0.40, fy - tri_h / 2), (fx - tri_h * 0.40, fy + tri_h / 2),
           (fx + tri_h * 0.47, fy)]
    rows = []
    for j in range(size):
        row = bytearray([0])
        py = (j + 0.5) * s
        gy = max(0.0, min(1.0, (py - 100) / 824))
        tile_col = tuple(t + (b - t) * gy for t, b in zip(TILE_TOP, TILE_BOT))
        amber = tuple(t + (b - t) * max(0.0, min(1.0, (py - (fy - fh / 2)) / fh))
                      for t, b in zip(AMBER_TOP, AMBER_BOT))
        for i in range(size):
            px = (i + 0.5) * s
            col, alpha = (0.0, 0.0, 0.0), 0.0
            # soft drop shadow under the tile
            ds = sd_round_box(px, py - 14, *tile)
            sh = 0.42 * max(0.0, 1.0 - max(0.0, ds) / 34.0) ** 2 if size >= 64 else 0.0
            col, alpha = (0.0, 0.0, 0.0), sh
            dt = sd_round_box(px, py, *tile)
            ct = cover(dt, aa)
            if ct > 0:
                c = tile_col
                # a hairline of light along the top rim
                rim = cover(abs(dt + 1.5 * s) - 0.9 * s, aa) * max(0.0, 1 - (py - 100) / 260) * 0.16
                c = over(c, RIM, rim)
                # the frame (outline) and the play triangle
                df = abs(sd_round_box(px, py, fx, fy, fw / 2 - stroke / 2, fh / 2 - stroke / 2,
                                      fr - stroke / 2)) - stroke / 2
                c = over(c, amber, cover(df, aa))
                c = over(c, amber, cover(sd_triangle(px, py, tri), aa))
                # composite the tile over the shadow
                a_out = ct + alpha * (1 - ct)
                col = tuple((cc * ct) / a_out for cc in c) if a_out else c
                alpha = a_out
            r, g, b = (max(0, min(255, round(v * 255))) for v in col)
            row += bytes((r, g, b, max(0, min(255, round(alpha * 255)))))
        rows.append(bytes(row))
    return rows


def png(path, size, rows):
    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xffffffff)
    raw = b"".join(rows)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)


def main():
    sizes = sorted({16, 32, 64, 128, 256, 512, 1024})
    rendered = {}
    for n in sizes:
        rendered[n] = render(n)
        print(f"  rendered {n}x{n}", flush=True)
    iconset = Path(tempfile.mkdtemp()) / "Studio.iconset"
    iconset.mkdir()
    for n in (16, 32, 128, 256, 512):
        png(iconset / f"icon_{n}x{n}.png", n, rendered[n])
        png(iconset / f"icon_{n}x{n}@2x.png", n * 2, rendered[n * 2])
    # what tauri.conf.json lists (and the 1024 master, for anything else)
    png(HERE / "32x32.png", 32, rendered[32])
    png(HERE / "128x128.png", 128, rendered[128])
    png(HERE / "128x128@2x.png", 256, rendered[256])
    png(HERE / "icon.png", 512, rendered[512])
    png(HERE / "icon-1024.png", 1024, rendered[1024])
    iconutil = shutil.which("iconutil") or "/usr/bin/iconutil"
    if not os.path.exists(iconutil):
        sys.exit("iconutil not found (macOS only); PNGs written, icon.icns not")
    subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
    shutil.rmtree(iconset.parent)
    print(f"wrote {HERE / 'icon.icns'}")


if __name__ == "__main__":
    main()
