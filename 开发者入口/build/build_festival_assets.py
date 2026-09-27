# -*- coding: utf-8 -*-
"""节庆素材生成器（参数化，遍历 tools.festival_themes._FESTIVAL_REGISTRY）。

用户口径 2026-09-27：取代一次性「中秋」脚本，改为按注册表为每个节日程序化生成
气泡皮肤（<theme>-bubble.png，320x160）+ 飘落物（<theme>-<motif>.png，96x96）。
配色与 motif 全部来自注册表，故加节日无需改本脚本。

产出目录：用户端特用/web/icons/festival/（由 /icons/ 路由免鉴权 serve）。
运行：python build_festival_assets.py
依赖：Pillow（PIL）
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "通用工具"))
from tools.festival_themes import _FESTIVAL_REGISTRY  # noqa: E402

OUT = os.path.join(ROOT, "用户端特用", "web", "icons", "festival")
os.makedirs(OUT, exist_ok=True)

RAD = math.radians


# ---------------------------------------------------------------- 通用底图
def radial_glow(size, color, inner=0.0, outer=1.0, alpha=255):
    """圆形径向渐变（光晕）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    c = size / 2.0
    for y in range(size):
        for x in range(size):
            d = math.hypot(x - c + .5, y - c + .5) / c
            if d > outer or d < inner:
                continue
            t = (d - inner) / max(1e-6, (outer - inner))
            a = int(alpha * (1 - t) ** 1.6)
            if a > 0:
                px[x, y] = (color[0], color[1], color[2], a)
    return img


def _flower(img, color, accent, petals, r_factor):
    """通用花瓣花（梅花/桂花/菊花/雏菊共用，petals 控制数量与形态）。"""
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * r_factor
    for i in range(petals):
        a = RAD(i * 360 / petals - 90)
        fx = cx + r * math.cos(a)
        fy = cy + r * math.sin(a)
        d.ellipse([fx - r * 0.5, fy - r * 0.5, fx + r * 0.5, fy + r * 0.5],
                  fill=color, outline=accent)
    d.ellipse([cx - r * 0.28, cy - r * 0.28, cx + r * 0.28, cy + r * 0.28], fill=accent)


# ---------------------------------------------------------------- 各 motif 绘制
def draw_lantern(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.30
    d.ellipse([cx - r, cy - r * 1.1, cx + r, cy + r * 1.1], fill=color, outline=accent)
    d.rectangle([cx - r * 0.5, cy - r * 1.28, cx + r * 0.5, cy - r * 1.06], fill=accent)
    d.rectangle([cx - r * 0.5, cy + r * 1.06, cx + r * 0.5, cy + r * 1.28], fill=accent)
    d.line([cx, cy + r * 1.28, cx, cy + r * 1.5], fill=accent, width=2)
    d.ellipse([cx - r * 0.55, cy - r * 0.55, cx - r * 0.1, cy - r * 0.1],
              fill=accent + (110,))


def draw_tsuan(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.34
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=accent)
    d.ellipse([cx - r * 0.6, cy - r * 0.6, cx - r * 0.1, cy - r * 0.1], fill=accent + (80,))


def draw_glow(img, color, accent):
    S = img.size[0]
    g = radial_glow(S, color, inner=0.05, outer=1.0, alpha=210)
    a = radial_glow(S, accent, inner=0.0, outer=0.5, alpha=120)
    return Image.alpha_composite(g, a)


def draw_willow(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.34
    d.ellipse([cx - r, cy - r * 0.42, cx + r, cy + r * 0.42], fill=color, outline=accent)
    d.line([cx - r, cy, cx + r, cy], fill=accent, width=2)


def draw_rain(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    d.line([S * 0.22, S * 0.12, S * 0.78, S * 0.88], fill=color + (200,), width=3)
    d.line([S * 0.42, S * 0.12, S * 0.98, S * 0.88], fill=accent + (160,), width=2)


def draw_zongzi(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = S / 2
    pts = [(cx, S * 0.15), (S * 0.18, S * 0.82), (S * 0.82, S * 0.82)]
    d.polygon(pts, fill=color, outline=accent)
    d.line([cx, S * 0.15, cx, S * 0.82], fill=accent, width=3)
    d.line([S * 0.35, S * 0.5, S * 0.65, S * 0.5], fill=accent, width=3)


def draw_boat(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    d.ellipse([S * 0.15, S * 0.5, S * 0.85, S * 0.82], fill=color)
    d.rectangle([S * 0.45, S * 0.2, S * 0.55, S * 0.5], fill=accent)
    d.line([S * 0.5, S * 0.2, S * 0.78, S * 0.36], fill=accent, width=3)


def draw_star(img, color, accent, points=5):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r_out = S * 0.38
    r_in = r_out * 0.42
    pts = []
    for i in range(points * 2):
        ang = RAD(-90 + i * 180 / points)
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=color, outline=accent)


def draw_magpie(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    d.ellipse([S * 0.3, S * 0.45, S * 0.7, S * 0.72], fill=color)
    d.ellipse([S * 0.55, S * 0.3, S * 0.72, S * 0.5], fill=color)
    d.polygon([(S * 0.7, S * 0.4), (S * 0.86, S * 0.42), (S * 0.7, S * 0.48)], fill=accent)
    d.line([S * 0.3, S * 0.55, S * 0.12, S * 0.4], fill=color, width=4)


def draw_moon(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.36
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=accent)
    for (ox, oy, rr) in ((0.4, 0.2, 0.16), (0.5, 0.4, 0.12), (0.3, 0.45, 0.1)):
        d.ellipse([cx - r * ox - r * rr, cy - r * oy - r * rr,
                   cx - r * ox + r * rr, cy - r * oy + r * rr], fill=accent + (120,))


def draw_plum(img, color, accent):
    _flower(img, color, accent, petals=5, r_factor=0.24)


def draw_osmanthus(img, color, accent):
    _flower(img, color, accent, petals=4, r_factor=0.24)


def draw_rabbit(img, color, accent):
    S = img.size[0]
    sc = S / 64.0
    d = ImageDraw.Draw(img)
    def e(x0, y0, x1, y1, fill):
        d.ellipse([x0 * sc, y0 * sc, x1 * sc, y1 * sc], fill=fill)
    e(20, 34, 48, 56, color)   # 身体
    e(34, 24, 48, 38, color)   # 头
    e(36, 12, 42, 28, color)   # 左耳
    e(43, 12, 49, 28, color)   # 右耳
    e(16, 38, 26, 48, color)   # 尾
    d.ellipse([40 * sc, 28 * sc, 44 * sc, 32 * sc], fill=accent + (140,))  # 眼


def draw_chrys(img, color, accent):
    _flower(img, color, accent, petals=12, r_factor=0.30)


def draw_cloud(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.22
    for (dx, dy) in ((-0.3, 0.1), (0, 0), (0.3, 0.1), (0.15, 0.25), (-0.15, 0.25)):
        d.ellipse([cx + r * dx - r * 0.6, cy + r * dy - r * 0.5,
                   cx + r * dx + r * 0.6, cy + r * dy + r * 0.5], fill=color + (200,))


def draw_snow(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.38
    for i in range(6):
        a = RAD(i * 60)
        ex = cx + r * math.cos(a)
        ey = cy + r * math.sin(a)
        d.line([cx, cy, ex, ey], fill=color, width=3)
        mx = cx + r * 0.6 * math.cos(a)
        my = cy + r * 0.6 * math.sin(a)
        for s in (-1, 1):
            ba = RAD(i * 60 + 60 * s)
            d.line([mx, my, mx + r * 0.25 * math.cos(ba), my + r * 0.25 * math.sin(ba)],
                   fill=color, width=2)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=accent)


def draw_dumpling(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.34
    d.ellipse([cx - r, cy - r * 0.6, cx + r, cy + r * 0.9], fill=color, outline=accent)
    d.line([cx - r, cy - r * 0.4, cx + r, cy - r * 0.4], fill=accent, width=2)
    for k in range(4):
        x = cx - r + (2 * r) * (k + 0.5) / 4
        d.line([x, cy - r * 0.55, x, cy - r * 0.2], fill=accent, width=2)


def draw_firework(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    r = S * 0.4
    for i in range(12):
        a = RAD(i * 30)
        ex = cx + r * math.cos(a)
        ey = cy + r * math.sin(a)
        d.line([cx, cy, ex, ey], fill=color, width=3)
        d.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=accent)


def draw_ribbon(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    d.line([S * 0.15, S * 0.7, S * 0.5, S * 0.3, S * 0.85, S * 0.7], fill=color, width=6)
    d.line([S * 0.15, S * 0.85, S * 0.5, S * 0.45, S * 0.85, S * 0.85], fill=accent, width=6)


def draw_flag(img, color, accent):
    S = img.size[0]
    d = ImageDraw.Draw(img)
    d.rectangle([S * 0.2, S * 0.2, S * 0.8, S * 0.55], fill=color)
    d.line([S * 0.2, S * 0.2, S * 0.2, S * 0.85], fill=accent, width=4)
    d.ellipse([S * 0.45, S * 0.32, S * 0.6, S * 0.47], fill=accent)


DRAWERS = {
    "lantern": draw_lantern, "tsuan": draw_tsuan, "glow": draw_glow,
    "willow": draw_willow, "rain": draw_rain, "zongzi": draw_zongzi,
    "boat": draw_boat, "star": draw_star, "magpie": draw_magpie,
    "moon": draw_moon, "osmanthus": draw_osmanthus, "rabbit": draw_rabbit,
    "plum": draw_plum,
    "chrys": draw_chrys, "cloud": draw_cloud, "snow": draw_snow,
    "dumpling": draw_dumpling, "firework": draw_firework, "ribbon": draw_ribbon,
    "flag": draw_flag,
}


def make(motif, color, accent, size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fn = DRAWERS.get(motif)
    if fn:
        out = fn(img, color, accent)
        if out is not None:
            img = out
    return img.filter(ImageFilter.GaussianBlur(0.5))


def draw_bubble(W, H, top, bottom, accent, fall0):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for y in range(H):
        t = y / (H - 1)
        for x in range(W):
            tt = min(1.0, t + x / W * 0.12)
            r = int(top[0] + (bottom[0] - top[0]) * tt)
            g = int(top[1] + (bottom[1] - top[1]) * tt)
            b = int(top[2] + (bottom[2] - top[2]) * tt)
            px[x, y] = (r, g, b, 255)
    db = ImageDraw.Draw(img)
    for i, (oy, al) in enumerate(((16, 40), (34, 30), (52, 22))):
        db.arc([-40 + i * 16, oy, W - 60 + i * 12, oy + 96], 200, 340,
               fill=accent + (al,), width=2)
    # 角标：用该节日首个飘落 motif 小图，低透明度点缀，呼应主题
    if fall0 and fall0.get("motif") in DRAWERS:
        em = make(fall0["motif"], fall0["color"], fall0["accent"], 56)
        img.alpha_composite(em, (18, H - 74))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def save(img, name):
    p = os.path.join(OUT, name)
    img.save(p)
    print("  %-26s %5d x %-5d  %6.1f KB" % (name, img.size[0], img.size[1],
                                            os.path.getsize(p) / 1024))


def main():
    print("节庆素材生成 -> %s" % OUT)
    for theme, entry in _FESTIVAL_REGISTRY.items():
        a = entry.get("assets", {})
        dr = entry.get("draw", {})
        print("[%s]" % theme)
        if a.get("bubble"):
            bub = dr.get("bubble", {})
            save(draw_bubble(320, 160, bub.get("top", (255, 255, 255)),
                             bub.get("bottom", (240, 240, 240)),
                             bub.get("accent", (200, 200, 200)),
                             (a.get("fall") or [None])[0]), a["bubble"])
        for f in a.get("fall", []):
            save(make(f["motif"], f["color"], f["accent"], 96), f["file"])
    print("完成。")


if __name__ == "__main__":
    main()
