"""
表情差分头部裁剪（一次性脚本）

目的：把 知识库/图片素材/表情差分/ 下的半身立绘裁成「头部占满画面 + 极少脖子」的方形头像，
供用户端阿卡莉头像使用（头大、脖子极少，整体放大观感，不再显示半身）。

策略：
  1. 仅处理中文命名的差分文件（其余视为旧数据/杂物跳过）。
  2. 用 alpha（RGBA）或角落背景色阈值（RGB）定位「前景」包围盒。
  3. 解剖学定位头底（避免「上 X% 高度」在戴帽/特写/半身三种构图下表现不一）：
       a. 肩膀行：在 bbox 下 35% 区域（y0+0.65h 起）扫最大前景宽 —— 避开脸部误命中。
       b. 脸中心 ≈ y_mid（垂直中点）。
       c. head_bottom = y_mid + 0.55 × (y_shoulder - y_mid)
          → 半身立绘：干净切掉衣领/肩膀，头部填满 512 画面（脖子 ≈ 6%）；
          → 特写立绘：保留约 15% 脖子 + 极少领子（头 85% / 脖子 15% ≈ 用户目标）。
  4. 以 head_bottom - y0 作为方形边长 → 头填满画面、脖子极短。
  5. RGBA 合成白底，避免半透明像素残影。
  6. 输出到 表情差分/头像/ 子目录（保留原图不动）。

可重复执行：相同输入 → 相同输出（确定性裁剪）。
"""
import os
import sys
from PIL import Image

ROOT = r"知识库/图片素材/表情差分"
OUT = os.path.join(ROOT, "头像")

TARGET_SIZE = 512       # 输出方形尺寸
HEAD_HEIGHT_RATIO = 0.38  # 取前景 bbox 上 38% 作为头部区域（= 头 + 极少脖子，头更满）
PAD_NECK_PX = 10        # 脖子方向额外多取 ~10px（极少）
ALPHA_THRESH = 24       # alpha 阈值：>=24 视为前景
COLOR_DIFF = 38         # RGB 与角落背景色差阈值
SAMPLE_STEP = 2         # RGB 模式采样步长（速度）

def is_meaningful_name(stem: str) -> bool:
    """仅处理中文命名（其余视为旧数据/杂物）。"""
    return any('\u4e00' <= ch <= '\u9fff' for ch in stem)

def foreground_bbox(im: Image.Image):
    """返回前景 bbox (x0,y0,x1,y1)。RGBA 走 alpha，RGB 走角落背景色阈值。"""
    w, h = im.size
    if im.mode in ("RGBA", "LA") or "transparency" in im.info:
        alpha = im.convert("RGBA").split()[-1]
        # 用阈值扫描定位 bbox
        min_x, min_y, max_x, max_y = w, h, -1, -1
        for y in range(h):
            row = [alpha.getpixel((x, y)) for x in range(0, w, SAMPLE_STEP)]
            for i, a in enumerate(row):
                if a >= ALPHA_THRESH:
                    x = i * SAMPLE_STEP
                    if x < min_x: min_x = x
                    if y < min_y: min_y = y
                    if x > max_x: max_x = x
                    if y > max_y: max_y = y
        if max_x < 0:
            return None
        # 边界细化（用整像素扫描 x 范围）
        for x in range(min_x, max_x + 1, SAMPLE_STEP):
            col = [alpha.getpixel((x, y)) for y in range(min_y, max_y + 1)]
            if any(a >= ALPHA_THRESH for a in col):
                pass  # 已有 min/max_x
        return (min_x, min_y, max_x, max_y)
    # RGB：角落采样背景色，色差阈值定位前景
    rgb = im.convert("RGB")
    corners = [rgb.getpixel((0, 0)), rgb.getpixel((w - 1, 0)),
               rgb.getpixel((0, h - 1)), rgb.getpixel((w - 1, h - 1))]
    bg = max(set(corners), key=corners.count)
    br, bgc, bb = bg
    min_x, min_y, max_x, max_y = w, h, -1, -1
    px = rgb.load()
    for y in range(0, h, SAMPLE_STEP):
        for x in range(0, w, SAMPLE_STEP):
            r, g, b = px[x, y]
            if abs(r - br) + abs(g - bgc) + abs(b - bb) > COLOR_DIFF * 3:
                if x < min_x: min_x = x
                if y < min_y: min_y = y
                if x > max_x: max_x = x
                if y > max_y: max_y = y
    if max_x < 0:
        return None
    return (min_x, min_y, max_x, max_y)

def _row_width_alpha(apx, y, x0, x1, step=2):
    """alpha 图：在 [x0,x1] 行内找最左/最右前景像素，返回 (width, x_left, x_right)。"""
    l = r = None
    for x in range(x0, x1 + 1, step):
        if apx[x, y] >= ALPHA_THRESH:
            if l is None:
                l = x
            r = x
    return ((r - l) if l is not None else 0, l, r)


def _row_width_rgb(rpx, y, x0, x1, br, bgc, bb, step=2):
    """RGB 图：在 [x0,x1] 行内找最左/最右非背景像素，返回 (width, x_left, x_right)。"""
    l = r = None
    for x in range(x0, x1 + 1, step):
        r0, g0, b0 = rpx[x, y]
        if abs(r0 - br) + abs(g0 - bgc) + abs(b0 - bb) > COLOR_DIFF * 3:
            if l is None:
                l = x
            r = x
    return ((r - l) if l is not None else 0, l, r)


def find_head_bottom(im, char_bbox):
    """在人物 bbox 的「下 35%」找肩膀行（最大前景宽）→ 返回 (y_shoulder, y_mid)。
    自适应不同构图（戴帽/不戴帽、半身/特写），避开脸部误命中。
    下游 head_crop 据此按解剖学比例（face_center + 0.55 × (shoulder - face_center)）
    切出 head_bottom，使特写立绘脖子≈15%、半身立绘干净切掉衣领。"""
    x0, y0, x1, y1 = char_bbox
    h = y1 - y0
    y_mid = y0 + h // 2
    # 只在下 35% 扫（>= y0 + 0.65h），跳过脸/发/帽的最大宽
    y_scan_start = y0 + int(0.65 * h)
    y_shoulder = y_scan_start
    max_w = 0
    if im.mode in ("RGBA", "LA") or "transparency" in im.info:
        a = im.convert("RGBA").split()[-1]
        apx = a.load()
        for y in range(y_scan_start, y1 + 1):
            w, _, _ = _row_width_alpha(apx, y, x0, x1)
            if w > max_w:
                max_w = w
                y_shoulder = y
    else:
        rgb = im.convert("RGB")
        rpx = rgb.load()
        corners = [rpx[0, 0], rpx[im.width - 1, 0],
                   rpx[0, im.height - 1], rpx[im.width - 1, im.height - 1]]
        bg = max(set(corners), key=corners.count)
        br, bgc, bb = bg
        for y in range(y_scan_start, y1 + 1):
            w, _, _ = _row_width_rgb(rpx, y, x0, x1, br, bgc, bb)
            if w > max_w:
                max_w = w
                y_shoulder = y
    return y_shoulder, y_mid


def head_crop(im: Image.Image) -> Image.Image:
    """裁成「头部占满画面 + 极少脖子」方形；不足时裁小，超界时夹紧。
    自适应：解剖学定位肩膀行 + 脸中心，按 face_center+0.55×(shoulder-face_center)
    切头底 → 头填满画面、脖子极短。"""
    bbox = foreground_bbox(im)
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    h = y1 - y0
    y_shoulder, y_mid = find_head_bottom(im, bbox)
    # 解剖学头底：脸中心 → 肩膀方向 55%（夹紧：[y_mid, y_shoulder-5]）
    head_bottom = int(y_mid + 0.55 * (y_shoulder - y_mid))
    head_bottom = max(y_mid, min(head_bottom, y_shoulder - 5, y1))
    # 头部区域 = [y0, head_bottom]
    head_h = head_bottom - y0
    # 以头部区域高度作为方形边长（关键：让头部填满 512 画面，整体放大、少露脖子）
    side = min(head_h, im.width, im.height)
    cx = (x0 + x1) // 2
    cy = y0 + head_h // 2
    x_left = max(0, cx - side // 2)
    y_top = max(0, cy - side // 2)
    x_right = min(im.width, x_left + side)
    y_bottom = min(im.height, y_top + side)
    if x_right - x_left < side:
        x_left = max(0, x_right - side)
    if y_bottom - y_top < side:
        y_top = max(0, y_bottom - side)
    # 二次夹紧（极端图保护）
    x_left = max(0, min(x_left, im.width - side))
    y_top = max(0, min(y_top, im.height - side))
    return im.crop((x_left, y_top, x_left + side, y_top + side))

def process(src_path: str, dst_path: str):
    im = Image.open(src_path)
    crop = head_crop(im)
    if crop is None:
        return False, "前景未检出"
    # RGBA → RGB 合成白底，避免头像背景透出杂色
    if crop.mode == "RGBA":
        bg = Image.new("RGB", crop.size, (255, 255, 255))
        bg.paste(crop, mask=crop.split()[-1])
        crop = bg
    else:
        crop = crop.convert("RGB")
    crop = crop.resize((TARGET_SIZE, TARGET_SIZE), Image.LANCZOS)
    crop.save(dst_path, optimize=True)
    return True, f"{crop.size[0]}x{crop.size[1]}"

def main():
    os.makedirs(OUT, exist_ok=True)
    n_ok = n_skip = n_fail = 0
    for f in sorted(os.listdir(ROOT)):
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p):
            continue
        stem, ext = os.path.splitext(f)
        if not is_meaningful_name(stem):
            n_skip += 1
            continue
        out_name = stem + ".jpg"  # 输出统一 jpg，体积小
        dst = os.path.join(OUT, out_name)
        try:
            ok, info = process(p, dst)
        except Exception as e:
            ok, info = False, f"{type(e).__name__}: {e}"
        if ok:
            n_ok += 1
            print(f"  ✓ {f:20s} -> {out_name:20s} {info}")
        else:
            n_fail += 1
            print(f"  ✗ {f:20s} 失败: {info}")
    print(f"\n生成 {n_ok} | 跳过(非中文) {n_skip} | 失败 {n_fail}")
    print(f"输出目录: {OUT}")

if __name__ == "__main__":
    main()