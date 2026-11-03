"""
生成 Spark 的图标：粗壮的白色四角星芒，落在蓝→深海军蓝渐变的圆角方块上，带一点立体感。

蓝色跟界面里的强调色是一家，图标顺着这套色走，不另起一套。星芒是 x=R·cos^p θ、y=R·sin^p θ 的曲线——四个尖、边微微内凹，
p 取得小，身子粗，16px 下仍然认得出，不会糊成一团。

    python3 tools/make_icon.py          # 重新生成 assets/ 下的图标

产物：assets/Spark.icns（给 .app 用）、assets/icon-512.png、assets/icon-64.png
（给网页 favicon 用），以及手机 App 的图标（同一张画，不切圆角、铺满整块，
iOS 自己按系统的形状切）。
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
IOS_ICON = os.path.join(ROOT, "apps", "mobile-ios", "Spark", "Assets.xcassets",
                        "AppIcon.appiconset", "icon-1024.png")

SS = 4                      # 超采样倍数：先画大的再缩小，边缘才平滑
SIZE = 1024
# 渐变：左上中蓝、右下深海军蓝（NordVPN 那一路），跟手机 App 里的强调色 #1e4a8c 同一个蓝色家族。
# 试过提亮成天蓝（太浅）、深紫→深蓝（紫色在 App 里没有呼应），都不如这个
TOP = (54, 110, 240)        # #366ef0
BOTTOM = (18, 36, 120)      # #122478
INK = (8, 16, 64)           # 投影的颜色：比底色深的海军蓝，不用灰黑，免得发脏


def _gradient(size: int) -> Image.Image:
    """左上紫、右下蓝的对角渐变。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x / size * 0.45 + y / size * 0.55)
            px[x, y] = tuple(
                round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)
            )
    return img


def _squircle_mask(size: int) -> Image.Image:
    """macOS 风格的圆角方块。半径取 22.4%，接近系统图标的观感。"""
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    pad = round(size * 0.055)          # 四周留一点空，图标不顶满画布
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=round(size * 0.224), fill=255)
    return mask


def _spark(size: int, r_ratio: float, cx: float, cy: float, power: float = 2.5):
    """星芒的顶点序列：x=R·cos^p θ, y=R·sin^p θ。p=2 是菱形，越大边越往里凹、尖越细；
    取 2.7～2.9：比菱形多一点内凹，看得出是星芒，身子又比原来的 3.6 粗壮得多。"""
    R = size * r_ratio
    pts = []
    for i in range(720):
        th = i / 720 * 2 * math.pi
        c, s = math.cos(th), math.sin(th)
        pts.append((
            cx + R * math.copysign(abs(c) ** power, c),
            cy + R * math.copysign(abs(s) ** power, s),
        ))
    return pts


def _art(big: int) -> Image.Image:
    """渐变底 + 星芒，还没切圆角。立体感只加一点：底子左上一团高光，星芒下面一层柔和的投影，
    星芒本身从纯白过渡到很淡的蓝。"""
    base = _gradient(big)

    glow = Image.new("L", (big, big), 0)
    ImageDraw.Draw(glow).ellipse([-big * 0.3, -big * 0.4, big * 0.7, big * 0.5], fill=60)
    glow = glow.filter(ImageFilter.GaussianBlur(big * 0.14))
    base = Image.composite(Image.new("RGB", (big, big), (255, 255, 255)), base, glow)

    cx = cy = big / 2
    shapes = [  # (半径, 圆心 x, 圆心 y, 指数)：主星芒偏左下，右上角一颗小的
        (0.355, cx - big * 0.035, cy + big * 0.035, 2.9),
        (0.125, cx + big * 0.225, cy - big * 0.225, 2.7),
    ]
    mask = Image.new("L", (big, big), 0)
    md = ImageDraw.Draw(mask)
    for r, x, y, pw in shapes:
        md.polygon(_spark(big, r, x, y, pw), fill=255)

    # 投影：往下挪一点、虚化，叠成深一些的蓝紫
    shadow = Image.new("L", (big, big), 0)
    shadow.paste(mask, (0, round(big * 0.022)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(big * 0.028)).point(lambda v: v * 0.45)
    base = Image.composite(Image.new("RGB", (big, big), INK), base, shadow)

    # 星芒：上白下淡蓝
    fill = Image.new("RGB", (big, big))
    fd = ImageDraw.Draw(fill)
    for y in range(big):
        t = y / big
        fd.line([(0, y), (big, y)], fill=(round(255 - 22 * t), round(255 - 14 * t), 255))
    return Image.composite(fill, base, mask)


def render(size: int) -> tuple[Image.Image, Image.Image]:
    """(桌面版：圆角方块、四周透明, 手机版：铺满、不带透明)"""
    big = size * SS
    art = _art(big)
    icon = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    icon.paste(art, (0, 0), _squircle_mask(big))
    return (icon.resize((size, size), Image.LANCZOS),
            art.resize((size, size), Image.LANCZOS))


def main() -> None:
    os.makedirs(ASSETS, exist_ok=True)
    master, ios = render(SIZE)
    ios.save(IOS_ICON)            # App Store 要求 1024、不能有透明通道

    master.resize((512, 512), Image.LANCZOS).save(os.path.join(ASSETS, "icon-512.png"))
    master.resize((64, 64), Image.LANCZOS).save(os.path.join(ASSETS, "icon-64.png"))

    iconset = os.path.join(ASSETS, "Spark.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for base_size in (16, 32, 128, 256, 512):
        master.resize((base_size, base_size), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{base_size}x{base_size}.png"))
        master.resize((base_size * 2, base_size * 2), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{base_size}x{base_size}@2x.png"))

    icns = os.path.join(ASSETS, "Spark.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(iconset)
    # 网页 favicon：三个页面都用同一枚图标（它们本来就是同一个服务）
    for dest in ("static", os.path.join("apps", "summit2md", "static"),
                 os.path.join("apps", "notes2insight", "static")):
        shutil.copy(os.path.join(ASSETS, "icon-64.png"),
                    os.path.join(ROOT, dest, "icon.png"))

    print(f"生成好了：{icns}")
    print(f"          {os.path.join(ASSETS, 'icon-512.png')}")
    print(f"          {os.path.join(ASSETS, 'icon-64.png')}")
    print(f"          {IOS_ICON}")


if __name__ == "__main__":
    sys.exit(main())
