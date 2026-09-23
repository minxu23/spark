"""
生成 Spark 的图标：一枚四角星芒，落在紫→蓝的圆角方块上。

紫和蓝正是界面里 Summit 和 Podcast 两个模式的强调色，图标顺着这套色走，
不另起一套。星芒用 astroid 曲线（x=R·cos³θ, y=R·sin³θ）——四个尖、边内凹，
这个形状在 16px 下仍然认得出，不会糊成一团。

    python3 tools/make_icon.py          # 重新生成 assets/ 下的图标

产物：assets/Spark.icns（给 .app 用）、assets/icon-512.png、assets/icon-64.png
（给网页 favicon 用）。
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

SS = 4                      # 超采样倍数：先画大的再缩小，边缘才平滑
SIZE = 1024
PURPLE = (90, 56, 202)      # #5a38ca —— Summit 的紫
BLUE = (31, 95, 184)        # #1f5fb8 —— Podcast 的蓝


def _gradient(size: int) -> Image.Image:
    """左上紫、右下蓝的对角渐变。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x / size * 0.45 + y / size * 0.55)
            px[x, y] = tuple(
                round(PURPLE[i] + (BLUE[i] - PURPLE[i]) * t) for i in range(3)
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


def _spark(size: int, r_ratio: float, cx: float, cy: float, power: float = 3.6):
    """astroid 星芒的顶点序列：边内凹、四个尖。power 越大尖越细。"""
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


def render(size: int) -> Image.Image:
    big = size * SS
    base = _gradient(big)

    # 左上角一点高光，让平涂的渐变有点体积感
    glow = Image.new("L", (big, big), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-big * 0.25, -big * 0.35, big * 0.75, big * 0.55], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(big * 0.12))
    base = Image.composite(Image.new("RGB", (big, big), (255, 255, 255)), base, glow)

    layer = ImageDraw.Draw(base)
    cx = cy = big / 2
    # 主星芒偏左下一点，给右上角的小星芒让位
    layer.polygon(_spark(big, 0.330, cx - big * 0.030, cy + big * 0.030), fill=(255, 255, 255))
    # 小星芒：呼应"一堆内容压出一个要点"，也让构图不那么呆板
    layer.polygon(_spark(big, 0.120, cx + big * 0.225, cy - big * 0.225, 3.2),
                  fill=(255, 255, 255, 235))

    icon = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    icon.paste(base, (0, 0), _squircle_mask(big))
    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(ASSETS, exist_ok=True)
    master = render(SIZE)

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


if __name__ == "__main__":
    sys.exit(main())
