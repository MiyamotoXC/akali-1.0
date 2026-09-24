# -*- coding: utf-8 -*-
"""构建浏览器扩展：把「用户端本体」打包成可直接加载的 Chrome/Edge 扩展。

设计原则：**用户端网页是唯一真相源**。改完 用户端特用/web/index.html 后重跑本脚本，
扩展即同步（避免"网页改了、插件是旧版"的经典问题）。

本脚本做四件事：
  1) 内联脚本外置：页面里唯一的 <script>…</script> → app.js
     （Manifest V3 扩展页禁止内联脚本，必须外置，否则整页脚本不执行）
  2) 服务端占位符兜底：'__AKALI_BOOT_ID__' → '__AKALI_EXT__'
     （该占位符原本由服务端注入；扩展里没有服务端，改由 bridge.js 关闭自毁逻辑）
  3) 注入 <script src="bridge.js"> 到 <head> 最前
     （把服务器地址/口令交给用户端的跨域入口，必须先于页面脚本执行）
  4) 生成扩展图标 icons/icon{16,48,128}.png（源自 web/icons/icon-512.png）

用法：
    python 构建扩展.py
"""
import os
import re
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_HTML = os.path.join(ROOT, '用户端特用', 'web', 'index.html')
SRC_ICON = os.path.join(ROOT, '用户端特用', 'web', 'icons', 'icon-512.png')
OUT_HTML = os.path.join(HERE, 'app.html')
OUT_JS = os.path.join(HERE, 'app.js')
ICON_DIR = os.path.join(HERE, 'icons')


def main():
    if not os.path.isfile(SRC_HTML):
        print('!! 找不到用户端页面：%s' % SRC_HTML)
        return 1
    src = open(SRC_HTML, encoding='utf-8').read()
    src_size = len(src.encode('utf-8')) / 1024

    # ---- 1) 内联脚本外置（贪婪匹配到最后一个 </script>，页面只有一个 script 块）----
    m = re.search(r'<script>(.*)</script>', src, re.S)
    if not m:
        print('!! 未找到内联 <script>，请检查用户端页面结构是否变化')
        return 1
    open(OUT_JS, 'w', encoding='utf-8').write(m.group(1))
    src = src[:m.start()] + '<script src="app.js"></script>' + src[m.end():]

    # ---- 2) 服务端占位符兜底 ----
    src = src.replace("'__AKALI_BOOT_ID__'", "'__AKALI_EXT__'")

    # ---- 3) 注入 bridge.js（head 最前，保证最先执行）----
    if 'bridge.js' not in src:
        src = src.replace('<head>', '<head>\n<script src="bridge.js"></script>', 1)

    open(OUT_HTML, 'w', encoding='utf-8').write(src)

    # ---- 4) 图标 ----
    os.makedirs(ICON_DIR, exist_ok=True)
    made = []
    if os.path.isfile(SRC_ICON):
        im = Image.open(SRC_ICON).convert('RGB')
        for size in (16, 48, 128):
            p = os.path.join(ICON_DIR, 'icon%d.png' % size)
            im.resize((size, size), Image.LANCZOS).save(p, optimize=True)
            made.append((os.path.relpath(p, HERE), os.path.getsize(p) / 1024))
    else:
        print('!! 缺少图标源文件，请先运行 调试/_t_icons.py 生成 web/icons（或手动放入）')

    # ---- 报告 ----
    print('构建完成（用户端 → 扩展）：')
    print('  源页面  用户端特用/web/index.html  %.1f KB' % src_size)
    print('  app.html                        %.1f KB（已注入 bridge + 外置脚本）'
          % (os.path.getsize(OUT_HTML) / 1024))
    print('  app.js                          %.1f KB（原内联脚本）'
          % (os.path.getsize(OUT_JS) / 1024))
    for name, kb in made:
        print('  %-30s %.1f KB' % (name, kb))
    print('\n校验：')
    html = open(OUT_HTML, encoding='utf-8').read()
    print('  bridge.js 已注入          :', 'bridge.js' in html)
    print('  内联 <script> 已清除      :', '<script>' not in html)
    print('  BOOT_ID 占位符已兜底      :', '__AKALI_BOOT_ID__' not in html)
    return 0


if __name__ == '__main__':
    sys.exit(main())
