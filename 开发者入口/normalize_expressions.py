# -*- coding: utf-8 -*-
"""表情差分归一化：统一裁切基准，输出 512x512。

根治「头像有的大有的小」：前端 .msg .ava 是固定 70px 圆框 + object-fit:cover，
会把整张 512 画布等比缩进圆框，因此人物在画布里的占比/居中基准必须一致，
否则视觉上人物大小不一。

模式：
  center（默认，推荐）：纯几何中心裁正方形——二次元立绘实测 Haar 人脸检测
    大面积误检（舰装/头发/锚标被当人脸，目视全错），中心裁反而稳定可靠。
  face：Haar 人脸检测裁（仅备查/对比用，不推荐）。

输出：覆盖 知识库/图片素材/表情差分/头像/<stem>.jpg（前端 _resolve_expression_url 优先引用此目录）。
源图优先级：父目录中文命名 png（高分辨率原图）> 头像/ 下同名 jpg（自身）。

用法：
  python normalize_expressions.py                      # center 模式 dry-run
  python normalize_expressions.py --apply              # center 模式覆盖 头像/
  python normalize_expressions.py --apply --out <dir>  # 输出到独立目录（预览，不覆盖）
  python normalize_expressions.py --mode face ...      # 人脸检测模式（备查）
"""
import os
import sys
import glob
import argparse
import tempfile
import shutil

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(PROJECT_ROOT, "知识库")
EXP_DIR = os.path.join(KB_DIR, "图片素材", "表情差分")
HEAD_DIR = os.path.join(EXP_DIR, "头像")

OUT_SIZE = 512
EXPAND = 2.2  # 人脸框 → 正方形边长系数（含头+少量肩）；脸在输出占比 ≈ 1/EXPAND，固定即统一
JPEG_QUALITY = 95
# 头部特写参数（center 模式）：ZOOM=裁窗缩放（>1 放大人物），HEAD_Y=裁窗中心在图中的纵向比例
# 1.45 倍 + 中心 0.30H → 人物头部占据画面主体（此前 1.0/居中是半身像）
ZOOM = 1.45
HEAD_Y = 0.30
MIN_CROP = 220  # 裁窗边长下限（原图过小时不再放大，避免糊）

# Haar 级联：opencv 4.11 headless 把 haarcascade_*.xml 平铺在 cv2/data/ 下，
# cv2.data.haarcascades 已指向该目录（带尾斜杠），直接拼接文件名即可。
_DATA_DIR = cv2.data.haarcascades
_CASC_TMP = tempfile.mkdtemp(prefix="fx_haar_")


def _load_cascade(fname: str):
    src = os.path.join(_DATA_DIR, fname)
    dst = os.path.join(_CASC_TMP, fname)
    if not os.path.exists(dst):
        shutil.copy(src, dst)
    return cv2.CascadeClassifier(dst)


def _get_cascades():
    """惰性加载 Haar 级联（仅 face 模式需要；center 模式完全不依赖，避免中文路径/缺文件拖累）。"""
    global _CASCADES
    if _CASCADES is None:
        _CASCADES = [
            _load_cascade("haarcascade_frontalface_alt2.xml"),
            _load_cascade("haarcascade_frontalface_default.xml"),
            _load_cascade("haarcascade_profileface.xml"),
        ]
    return _CASCADES


_CASCADES = None


def _has_cjk(s: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' for ch in s)


def imread_utf(path: str):
    """Windows 中文路径安全读取。含 alpha 通道的 png（如 常态.png）透明区
    在 IMREAD_COLOR 下会变黑——按 UNCHANGED 读出后统一合成到白底。"""
    buf = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is not None and img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        rgb = img[:, :, :3].astype(np.float32)
        img = (rgb * alpha + 255.0 * (1.0 - alpha)).astype(np.uint8)
    elif img is not None and img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def imwrite_utf(path: str, img) -> bool:
    ext = os.path.splitext(path)[1].lower()
    ok, buf = cv2.imencode(ext if ext in ('.jpg', '.jpeg') else '.jpg', img)
    if not ok:
        return False
    with open(path, 'wb') as f:
        f.write(buf.tobytes())
    return True


def detect_face(gray: np.ndarray):
    """返回最大人脸框 (x,y,w,h) 或 None。正脸 alt2/default + 侧脸 profile 三级联，放宽参数提高二次元召回。
    注意：实测对二次元立绘大面积误检（舰装/头发/锚标被当人脸），故默认走 center 模式不调用本函数。"""
    for casc in _get_cascades():
        if casc.empty():
            continue
        rects = casc.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=3, minSize=(20, 20))
        if len(rects):
            areas = rects[:, 2] * rects[:, 3]
            x, y, w, h = rects[int(np.argmax(areas))]
            return int(x), int(y), int(w), int(h)
    return None


def crop_to_head(img: np.ndarray, box):
    """box=None → 中心裁（center 模式）：按 ZOOM 缩小裁窗、并把中心上移到 HEAD_Y，
    得到头部特写（用户口径 2026-09-09：头像要放大，不要半身像）。
    否则（face 模式）以人脸中心、边长=max(w,h)*EXPAND 的正方形（边缘复制填充越界）。"""
    H, W = img.shape[:2]
    if box is None:
        s = min(W, H)
        if ZOOM > 1.0:
            s = max(MIN_CROP, int(s / ZOOM))   # 缩小裁窗 = 放大人物；下限防糊
        cy = int(H * HEAD_Y) if ZOOM > 1.0 else H // 2
        x0 = max(0, min(W - s, (W - s) // 2))
        y0 = max(0, min(H - s, cy - s // 2))
        return img[y0:y0 + s, x0:x0 + s]
    x, y, w, h = box
    cx, cy = x + w // 2, y + h // 2
    L = int(max(w, h) * EXPAND)
    half = L // 2
    # 四周扩 half 边（复制边缘），再从扩边后坐标裁 L×L，自动处理越界
    pad = cv2.copyMakeBorder(img, half, half, half, half, cv2.BORDER_REPLICATE)
    nx0 = cy + half - half  # 扩边后原图中心 cy 对应 pad 坐标 cy+half；裁窗起点 = (cy+half)-half = cy
    ny0 = cx + half - half
    # 修正：扩边后原图(0,0)在(half,half)，故中心(cx,cy)在(cx+half, cy+half)
    px0 = cy + half - half  # == cy
    py0 = cx + half - half  # == cx
    crop = pad[py0:py0 + L, px0:px0 + L]
    if crop.shape[0] != L or crop.shape[1] != L:
        # 极端情况兜底：直接中心裁
        s = min(W, H)
        crop = img[(H - s) // 2:(H - s) // 2 + s, (W - s) // 2:(W - s) // 2 + s]
    return crop


def collect_stems():
    """待处理 stem：父目录中文命名图（png 优先，jpg/jpeg 亦可）+ 头像/ 现有图（去重）。
    注意：原始高分辨率 png 可能已被整理/移走，此时以父目录/头像下的中文名 jpg 为源。"""
    stems = {}
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        for f in glob.glob(os.path.join(EXP_DIR, ext)):
            stem = os.path.splitext(os.path.basename(f))[0]
            if _has_cjk(stem):
                stems.setdefault(stem, f)  # png 优先（先扫 png）
    for f in glob.glob(os.path.join(HEAD_DIR, "*.*")):
        stem = os.path.splitext(os.path.basename(f))[0]
        if _has_cjk(stem) and stem not in stems:
            stems[stem] = f
    return stems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='实际覆盖输出（默认 dry-run）')
    ap.add_argument('--out', default=None, help='输出目录（默认覆盖 HEAD_DIR）；预览时指定独立目录，不破坏原 头像/')
    ap.add_argument('--mode', choices=('center', 'face'), default='center',
                    help='center=纯几何中心裁（默认，推荐）；face=Haar 人脸检测裁（二次元立绘易误检，仅备查）')
    ap.add_argument('--zoom', type=float, default=ZOOM,
                    help='center 模式裁窗缩放（>1 放大人物，默认 1.45 头部特写；1.0=原居中半身）')
    ap.add_argument('--head-y', type=float, default=HEAD_Y,
                    help='center 模式裁窗中心纵向比例（默认 0.30，越小越偏头顶）')
    args = ap.parse_args()

    out_dir = args.out or HEAD_DIR
    os.makedirs(out_dir, exist_ok=True)
    stems = collect_stems()
    print(f"待处理表情差分：{len(stems)} 张  模式={args.mode}  apply={args.apply}\n")

    hit = 0
    for stem, src in sorted(stems.items()):
        img = imread_utf(src)
        if img is None:
            print(f"  [跳过] {stem}  读取失败：{src}")
            continue
        # center 模式（默认）：不调用人脸检测，直接中心正方形裁——避免二次元立绘误检导致裁歪
        box = detect_face(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)) if args.mode == 'face' else None
        crop = crop_to_head(img, box)
        out = cv2.resize(crop, (OUT_SIZE, OUT_SIZE), interpolation=cv2.INTER_LANCZOS4)
        status = "FACE" if box else ("CENTER" if args.mode == 'center' else "CENTER(fallback)")
        if box:
            hit += 1
        if args.apply:
            out_path = os.path.join(out_dir, stem + ".jpg")
            imwrite_utf(out_path, out)
            print(f"  [写] {stem:<12} {status:<16} src={os.path.basename(src)}  box={box} -> {OUT_SIZE}x{OUT_SIZE}")
        else:
            print(f"  [dry] {stem:<12} {status:<16} src={os.path.basename(src)}  box={box} -> {OUT_SIZE}x{OUT_SIZE}")

    if args.mode == 'face':
        print(f"\n人脸检测命中：{hit}/{len(stems)}  未命中回退中心裁：{len(stems)-hit}")
    else:
        print(f"\ncenter 模式：{len(stems)} 张全部按几何中心裁，未使用人脸检测")
    if not args.apply:
        print("（dry-run 未写文件，加 --apply 覆盖 头像/ 目录）")


if __name__ == "__main__":
    main()
