# -*- coding: utf-8 -*-
"""临时：生成中秋（节庆主题）素材。跑完即删。

产出到 用户端特用/web/icons/festival/（该目录已由 /icons/ 路由免鉴权 serve）：
  midautumn-ring.png       头像月桂金环（透明中心，叠在头像上沿）
  midautumn-bubble.png     阿卡莉气泡底纹（米金 + 云纹 + 月牙，平铺/拉伸皆可）
  midautumn-moon.png       飘落物：满月
  midautumn-osmanthus.png  飘落物：桂花
  midautumn-rabbit.png     飘落物：玉兔剪影（点缀）
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.stdout.reconfigure(encoding='utf-8')

OUT = r'c:\Users\Asus\Desktop\FX杂项\阿卡莉1.0\用户端特用\web\icons\festival'
os.makedirs(OUT, exist_ok=True)

GOLD = (245, 196, 81)
GOLD_DEEP = (214, 158, 46)
GOLD_PALE = (255, 233, 176)
CREAM = (255, 249, 235)
INK = (92, 76, 52)


def save(img, name):
    p = os.path.join(OUT, name)
    img.save(p)
    print('  %-26s %5d x %-5d  %6.1f KB' % (name, img.size[0], img.size[1], os.path.getsize(p) / 1024))


def radial_glow(size, color, inner=0.0, outer=1.0, alpha=255):
    """圆形径向渐变（用于光晕/月晕）。"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
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


# ---------------------------------------------------------------- 头像金环
# 思路：外圈月桂金环 + 四角桂花点缀，中心完全透明（叠在头像上只显环）
S = 256
ring = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(ring)
pad = 6
width = 9
d.ellipse([pad, pad, S - pad, S - pad], outline=GOLD_DEEP, width=width + 4)
d.ellipse([pad + 2, pad + 2, S - pad - 2, S - pad - 2], outline=GOLD, width=width)
d.ellipse([pad + 5, pad + 5, S - pad - 5, S - pad - 5], outline=GOLD_PALE, width=2)
# 四角桂花小簇
for ang in (45, 135, 225, 315):
    cx = S / 2 + (S / 2 - pad - width / 2) * math.cos(math.radians(ang))
    cy = S / 2 + (S / 2 - pad - width / 2) * math.sin(math.radians(ang))
    for i in range(4):
        a2 = math.radians(ang + i * 90)
        fx = cx + 7 * math.cos(a2)
        fy = cy + 7 * math.sin(a2)
        d.ellipse([fx - 5.5, fy - 5.5, fx + 5.5, fy + 5.5], fill=GOLD_PALE, outline=GOLD_DEEP)
    d.ellipse([cx - 3.2, cy - 3.2, cx + 3.2, cy + 3.2], fill=GOLD)
ring = ring.filter(ImageFilter.GaussianBlur(0.4))
glow = radial_glow(S, GOLD, inner=0.80, outer=1.0, alpha=110)
save(Image.alpha_composite(glow, ring), 'midautumn-ring.png')


# ---------------------------------------------------------------- 气泡底纹
# 思路：米金纵向渐变 + 右上月牙 + 淡云纹与桂花，整体低对比，不压文字
W, H = 320, 160
bub = Image.new('RGBA', (W, H), (0, 0, 0, 0))
px = bub.load()
for y in range(H):
    t = y / (H - 1)
    for x in range(W):
        tt = min(1.0, t + x / W * .12)
        r = int(CREAM[0] + (252 - CREAM[0]) * tt)
        g = int(CREAM[1] + (232 - CREAM[1]) * tt)
        b = int(252 - (252 - 196) * tt)
        px[x, y] = (r, g, b, 255)
db = ImageDraw.Draw(bub)
# 淡云纹（三层弧线）
for i, (oy, al) in enumerate(((16, 46), (34, 34), (52, 24))):
    db.arc([-40 + i * 16, oy, W - 60 + i * 12, oy + 96], 200, 340,
           fill=(214, 158, 46, al), width=2)
# 右上月牙
mx, my, mr = W - 52, 40, 26
db.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(245, 196, 81, 90))
db.ellipse([mx - mr + 11, my - mr - 6, mx + mr + 11, my + mr - 6], fill=(0, 0, 0, 0))
# 左下桂花点缀
for k in range(5):
    gx = 26 + k * 15
    gy = H - 26 - (k % 2) * 9
    for i in range(4):
        a2 = math.radians(i * 90)
        db.ellipse([gx + 5 * math.cos(a2) - 4, gy + 5 * math.sin(a2) - 4,
                    gx + 5 * math.cos(a2) + 4, gy + 5 * math.sin(a2) + 4],
                   fill=(255, 233, 176, 165))
bub = bub.filter(ImageFilter.GaussianBlur(0.6))
save(bub, 'midautumn-bubble.png')


# ---------------------------------------------------------------- 飘落：满月
M = 96
moon = Image.new('RGBA', (M, M), (0, 0, 0, 0))
dm = ImageDraw.Draw(moon)
dm.ellipse([10, 10, M - 10, M - 10], fill=(255, 240, 200, 235))
dm.ellipse([22, 26, 38, 42], fill=(240, 214, 150, 190))
dm.ellipse([54, 44, 70, 60], fill=(242, 220, 162, 165))
dm.ellipse([38, 60, 50, 72], fill=(244, 226, 178, 140))
moon = Image.alpha_composite(radial_glow(M, GOLD_PALE, inner=0.42, outer=1.0, alpha=95), moon)
save(moon, 'midautumn-moon.png')


# ---------------------------------------------------------------- 飘落：桂花
G = 64
osb = Image.new('RGBA', (G, G), (0, 0, 0, 0))
do = ImageDraw.Draw(osb)
cc = G / 2
for i in range(5):
    a2 = math.radians(i * 72 - 90)
    fx = cc + 13 * math.cos(a2)
    fy = cc + 13 * math.sin(a2)
    do.ellipse([fx - 10, fy - 10, fx + 10, fy + 10], fill=(255, 222, 130, 225))
do.ellipse([cc - 5, cc - 5, cc + 5, cc + 5], fill=(226, 168, 52, 235))
osb = osb.filter(ImageFilter.GaussianBlur(0.5))
save(osb, 'midautumn-osmanthus.png')


# ---------------------------------------------------------------- 飘落：玉兔剪影
R = 64
rab = Image.new('RGBA', (R, R), (0, 0, 0, 0))
dr = ImageDraw.Draw(rab)
dr.ellipse([20, 34, 48, 56], fill=(255, 246, 224, 170))          # 身体
dr.ellipse([34, 24, 48, 38], fill=(255, 246, 224, 170))          # 头
dr.ellipse([36, 12, 42, 28], fill=(255, 246, 224, 170))          # 左耳
dr.ellipse([43, 12, 49, 28], fill=(255, 246, 224, 170))          # 右耳
dr.ellipse([16, 38, 26, 48], fill=(255, 246, 224, 170))          # 尾
rab = rab.filter(ImageFilter.GaussianBlur(0.7))
save(rab, 'midautumn-rabbit.png')

print('中秋素材生成完毕 -> %s' % OUT)
