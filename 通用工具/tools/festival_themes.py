# -*- coding: utf-8 -*-
"""节庆特效：数据驱动的节日注册表（单一事实源）。

用户口径 2026-09-27：把原本写死在 web_app.py 的「中秋单节日」特例，升级为
「注册表 → 后端下发描述符 → 前端通用消费」的结构，覆盖主流传统节日 + 新年 + 国庆。

加节日只改本文件一处：登记 keywords（匹配 holiday_cn 的节日名）、assets（气泡皮肤 +
飘落物 motif）、draw（程序化出图用的原始 RGB 配色）、palette（前端注入的 CSS 变量）。
前端 applyFestivalTheme 不再有任何主题分支；web_app 的 /api/ui_assets 原样下发。

字段说明：
- keywords: list[str]  节日名子串，命中即采用该主题（如 '国庆' 命中 '国庆节'）
- assets.bubble: str   气泡皮肤 PNG 文件名（web/icons/festival/ 下）
- assets.fall:  list   飘落物，每项 {file, motif, color, accent, size:[min,max],
                            speed:[min,max], rotate:bool}
                            file 为 PNG 文件名；motif 为生成器的绘制键；
                            color/accent 为程序化出图 RGB；size/speed 为前端像素范围
- draw.bubble: {top,bottom,accent}  气泡渐变上下色 + 角标强调色（生成器用）
- palette: dict  CSS 自定义属性，前端注入 :root
          --fest-ring        头像环 box-shadow 颜色
          --fest-bubble-img  气泡背景图 url
          --fest-bubble-bg   气泡底色
          --fest-user-border 用户气泡描边色
"""
# 主题键 → 描述符
_FESTIVAL_REGISTRY = {
    # ---------------------------------------------------------------- 春节
    "spring": {
        "keywords": ["春节", "除夕", "大年", "过年"],
        "assets": {
            "bubble": "spring-bubble.png",
            "fall": [
                {"file": "spring-lantern.png", "motif": "lantern",
                 "color": (210, 40, 40), "accent": (245, 205, 120),
                 "size": [18, 34], "speed": [9, 16], "rotate": True},
                {"file": "spring-plum.png", "motif": "plum",
                 "color": (226, 120, 150), "accent": (255, 200, 215),
                 "size": [12, 24], "speed": [8, 15], "rotate": True},
            ],
        },
        "draw": {"bubble": {"top": (255, 244, 238), "bottom": (255, 222, 210), "accent": (214, 40, 40)}},
        "palette": {
            "--fest-ring": "rgba(214,40,40,.95)",
            "--fest-bubble-img": "url(/icons/festival/spring-bubble.png)",
            "--fest-bubble-bg": "#fff4ee",
            "--fest-user-border": "rgba(214,40,40,.7)",
        },
    },
    # ---------------------------------------------------------------- 元宵
    "lantern": {
        "keywords": ["元宵", "上元"],
        "assets": {
            "bubble": "lantern-bubble.png",
            "fall": [
                {"file": "lantern-tsuan.png", "motif": "tsuan",
                 "color": (255, 250, 245), "accent": (230, 210, 190),
                 "size": [16, 28], "speed": [9, 15], "rotate": False},
                {"file": "lantern-glow.png", "motif": "glow",
                 "color": (235, 120, 60), "accent": (255, 200, 120),
                 "size": [14, 26], "speed": [8, 14], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (255, 245, 235), "bottom": (255, 225, 205), "accent": (210, 90, 50)}},
        "palette": {
            "--fest-ring": "rgba(210,90,50,.95)",
            "--fest-bubble-img": "url(/icons/festival/lantern-bubble.png)",
            "--fest-bubble-bg": "#fff5eb",
            "--fest-user-border": "rgba(210,90,50,.7)",
        },
    },
    # ---------------------------------------------------------------- 清明
    "qingming": {
        "keywords": ["清明"],
        "assets": {
            "bubble": "qingming-bubble.png",
            "fall": [
                {"file": "qingming-willow.png", "motif": "willow",
                 "color": (130, 170, 100), "accent": (90, 130, 70),
                 "size": [14, 26], "speed": [8, 15], "rotate": True},
                {"file": "qingming-rain.png", "motif": "rain",
                 "color": (150, 180, 200), "accent": (200, 220, 235),
                 "size": [10, 20], "speed": [7, 13], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (240, 248, 240), "bottom": (220, 238, 222), "accent": (110, 150, 90)}},
        "palette": {
            "--fest-ring": "rgba(110,150,90,.9)",
            "--fest-bubble-img": "url(/icons/festival/qingming-bubble.png)",
            "--fest-bubble-bg": "#f0f8f0",
            "--fest-user-border": "rgba(110,150,90,.6)",
        },
    },
    # ---------------------------------------------------------------- 端午
    "dragonboat": {
        "keywords": ["端午", "龙舟"],
        "assets": {
            "bubble": "dragonboat-bubble.png",
            "fall": [
                {"file": "dragonboat-zongzi.png", "motif": "zongzi",
                 "color": (70, 150, 90), "accent": (230, 220, 160),
                 "size": [16, 28], "speed": [9, 15], "rotate": False},
                {"file": "dragonboat-boat.png", "motif": "boat",
                 "color": (60, 120, 150), "accent": (240, 220, 160),
                 "size": [20, 34], "speed": [10, 17], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (235, 248, 240), "bottom": (210, 238, 225), "accent": (40, 140, 90)}},
        "palette": {
            "--fest-ring": "rgba(40,140,90,.92)",
            "--fest-bubble-img": "url(/icons/festival/dragonboat-bubble.png)",
            "--fest-bubble-bg": "#eaf8f0",
            "--fest-user-border": "rgba(40,140,90,.65)",
        },
    },
    # ---------------------------------------------------------------- 七夕
    "qixi": {
        "keywords": ["七夕"],
        "assets": {
            "bubble": "qixi-bubble.png",
            "fall": [
                {"file": "qixi-star.png", "motif": "star",
                 "color": (255, 225, 140), "accent": (255, 245, 200),
                 "size": [12, 24], "speed": [8, 15], "rotate": True},
                {"file": "qixi-magpie.png", "motif": "magpie",
                 "color": (60, 60, 80), "accent": (220, 220, 235),
                 "size": [18, 30], "speed": [9, 16], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (245, 240, 250), "bottom": (228, 218, 245), "accent": (120, 90, 180)}},
        "palette": {
            "--fest-ring": "rgba(120,90,180,.9)",
            "--fest-bubble-img": "url(/icons/festival/qixi-bubble.png)",
            "--fest-bubble-bg": "#f5f0fa",
            "--fest-user-border": "rgba(120,90,180,.6)",
        },
    },
    # ---------------------------------------------------------------- 中秋（沿用既有风格）
    "midautumn": {
        "keywords": ["中秋"],
        "assets": {
            "bubble": "midautumn-bubble.png",
            "fall": [
                {"file": "midautumn-moon.png", "motif": "moon",
                 "color": (255, 240, 200), "accent": (245, 196, 81),
                 "size": [20, 36], "speed": [10, 17], "rotate": False},
                {"file": "midautumn-osmanthus.png", "motif": "osmanthus",
                 "color": (255, 222, 130), "accent": (226, 168, 52),
                 "size": [14, 24], "speed": [8, 15], "rotate": True},
                {"file": "midautumn-rabbit.png", "motif": "rabbit",
                 "color": (255, 246, 224), "accent": (245, 196, 81),
                 "size": [16, 26], "speed": [9, 16], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (255, 249, 235), "bottom": (252, 232, 196), "accent": (214, 158, 46)}},
        "palette": {
            "--fest-ring": "rgba(245,196,81,.95)",
            "--fest-bubble-img": "url(/icons/festival/midautumn-bubble.png)",
            "--fest-bubble-bg": "#fff9eb",
            "--fest-user-border": "rgba(214,158,46,.7)",
        },
    },
    # ---------------------------------------------------------------- 重阳
    "chongyang": {
        "keywords": ["重阳"],
        "assets": {
            "bubble": "chongyang-bubble.png",
            "fall": [
                {"file": "chongyang-chrys.png", "motif": "chrys",
                 "color": (240, 180, 60), "accent": (255, 225, 150),
                 "size": [16, 28], "speed": [9, 15], "rotate": True},
                {"file": "chongyang-cloud.png", "motif": "cloud",
                 "color": (255, 240, 220), "accent": (245, 210, 170),
                 "size": [18, 30], "speed": [10, 16], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (255, 248, 235), "bottom": (252, 232, 200), "accent": (220, 150, 40)}},
        "palette": {
            "--fest-ring": "rgba(220,150,40,.92)",
            "--fest-bubble-img": "url(/icons/festival/chongyang-bubble.png)",
            "--fest-bubble-bg": "#fff8eb",
            "--fest-user-border": "rgba(220,150,40,.65)",
        },
    },
    # ---------------------------------------------------------------- 冬至
    "dongzhi": {
        "keywords": ["冬至"],
        "assets": {
            "bubble": "dongzhi-bubble.png",
            "fall": [
                {"file": "dongzhi-snow.png", "motif": "snow",
                 "color": (235, 245, 255), "accent": (200, 225, 245),
                 "size": [14, 26], "speed": [8, 15], "rotate": True},
                {"file": "dongzhi-dumpling.png", "motif": "dumpling",
                 "color": (255, 250, 245), "accent": (230, 215, 195),
                 "size": [16, 28], "speed": [9, 15], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (242, 248, 252), "bottom": (220, 235, 245), "accent": (90, 140, 190)}},
        "palette": {
            "--fest-ring": "rgba(90,140,190,.9)",
            "--fest-bubble-img": "url(/icons/festival/dongzhi-bubble.png)",
            "--fest-bubble-bg": "#f2f8fc",
            "--fest-user-border": "rgba(90,140,190,.6)",
        },
    },
    # ---------------------------------------------------------------- 新年 / 元旦
    "newyear": {
        "keywords": ["元旦", "新年", "跨年"],
        "assets": {
            "bubble": "newyear-bubble.png",
            "fall": [
                {"file": "newyear-firework.png", "motif": "firework",
                 "color": (255, 120, 90), "accent": (255, 220, 120),
                 "size": [18, 32], "speed": [9, 16], "rotate": False},
                {"file": "newyear-ribbon.png", "motif": "ribbon",
                 "color": (80, 160, 220), "accent": (255, 200, 120),
                 "size": [16, 28], "speed": [8, 15], "rotate": True},
            ],
        },
        "draw": {"bubble": {"top": (255, 245, 240), "bottom": (255, 220, 210), "accent": (220, 40, 60)}},
        "palette": {
            "--fest-ring": "rgba(220,40,60,.95)",
            "--fest-bubble-img": "url(/icons/festival/newyear-bubble.png)",
            "--fest-bubble-bg": "#fff5f0",
            "--fest-user-border": "rgba(220,40,60,.7)",
        },
    },
    # ---------------------------------------------------------------- 国庆
    "national": {
        "keywords": ["国庆", "国庆节"],
        "assets": {
            "bubble": "national-bubble.png",
            "fall": [
                {"file": "national-star.png", "motif": "star",
                 "color": (255, 210, 60), "accent": (255, 240, 160),
                 "size": [14, 26], "speed": [8, 15], "rotate": True},
                {"file": "national-flag.png", "motif": "flag",
                 "color": (200, 30, 40), "accent": (255, 210, 60),
                 "size": [20, 34], "speed": [10, 17], "rotate": False},
            ],
        },
        "draw": {"bubble": {"top": (255, 240, 238), "bottom": (255, 215, 210), "accent": (200, 30, 40)}},
        "palette": {
            "--fest-ring": "rgba(200,30,40,.95)",
            "--fest-bubble-img": "url(/icons/festival/national-bubble.png)",
            "--fest-bubble-bg": "#fff0ee",
            "--fest-user-border": "rgba(200,30,40,.7)",
        },
    },
}


def resolve_festival(name):
    """按节日名匹配注册表，返回 {theme, assets, palette}；未命中返回 None。

    name 来自 holiday_cn._today_holiday（联网日历名或知识库名，如 '国庆节'/'中秋节'）。
    """
    nm = (name or "").strip()
    if not nm:
        return None
    for theme, entry in _FESTIVAL_REGISTRY.items():
        for kw in entry.get("keywords", []):
            if kw and kw in nm:
                return {"theme": theme,
                        "assets": entry.get("assets", {}),
                        "palette": entry.get("palette", {})}
    return None
