"""Build billnojay.com brand files (dark grey + crimson) into app/static/brand.

Run locally (Pillow and a Korean bold font are needed only here, not in
production):

    python scripts/make_brand_assets.py --font C:/Windows/Fonts/malgunbd.ttf
"""
from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw, ImageFont

BG = (26, 29, 35)          # #1a1d23
PANEL = (34, 38, 46)       # #22262e
LINE = (58, 63, 72)        # #3a3f48
TEXT = (243, 245, 247)     # #f3f5f7
SUB = (196, 201, 209)      # #c4c9d1
CRIMSON = (255, 77, 94)    # #ff4d5e
YELLOW = (255, 209, 102)   # #ffd166

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "brand")


def font(path, size):
    return ImageFont.truetype(path, size)


def icon(size, font_path, opaque=True):
    """Crimson rounded tile with dark 'IQ'; on a dark square when opaque."""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), BG + (255,) if opaque else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(s * (0.08 if opaque else 0.0))
    d.rounded_rectangle([pad, pad, s - pad - 1, s - pad - 1], radius=int(s * 0.22), fill=CRIMSON + (255,))
    f = font(font_path, int(s * 0.46))
    box = d.textbbox((0, 0), "IQ", font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1] - s * 0.01), "IQ", font=f, fill=BG + (255,))
    return img.resize((size, size), Image.LANCZOS)


def favicon_svg():
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#ff4d5e"/>'
        '<text x="32" y="44" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" '
        'font-size="31" font-weight="900" fill="#1a1d23">IQ</text></svg>\n'
    )


def og_cover(font_path, reg_path):
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y in range(H):  # soft vertical gradient BG -> PANEL
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(BG[i] + (PANEL[i] - BG[i]) * t) for i in range(3)))
    d.rectangle([0, 0, W, 8], fill=CRIMSON)

    logo = icon(64, font_path, opaque=False)
    img.paste(logo, (70, 62), logo)
    d.text((152, 70), "SportIQ", font=font(font_path, 40), fill=TEXT)
    pill_f = font(font_path, 22)
    d.rounded_rectangle([330, 72, 428, 112], radius=20, fill=CRIMSON)
    d.ellipse([346, 86, 358, 98], fill=TEXT)
    d.text((366, 77), "LIVE", font=pill_f, fill=BG)

    big = font(font_path, 66)
    d.text((70, 210), "실시간 라이브스코어", font=big, fill=TEXT)
    x2 = 70
    d.text((x2, 300), "리그 순위표", font=big, fill=CRIMSON)
    x2 += d.textlength("리그 순위표 ", font=big)
    d.text((x2, 300), "· 승부예측 투표", font=big, fill=TEXT)
    d.text((70, 404), "축구 · 야구 · 농구 · 배구 · 하키 경기 스코어와 순위를 가입 없이 바로",
           font=font(reg_path, 30), fill=SUB)

    d.line([(70, 494), (W - 70, 494)], fill=LINE, width=2)
    chip_f = font(font_path, 24)
    x = 70
    for label in ("라이브스코어", "리그 순위표", "승부예측 투표 · 응원톡"):
        w = d.textlength(label, font=chip_f)
        d.rounded_rectangle([x, 524, x + w + 40, 572], radius=12, fill=PANEL, outline=LINE, width=2)
        d.text((x + 20, 533), label, font=chip_f, fill=TEXT)
        x += w + 56
    site_f = font(font_path, 30)
    d.text((W - 70 - d.textlength("billnojay.com", font=site_f), 530), "billnojay.com", font=site_f, fill=YELLOW)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", required=True, help="bold font with Korean glyphs")
    ap.add_argument("--regular", help="regular font with Korean glyphs (default: --font)")
    a = ap.parse_args()
    reg = a.regular or a.font
    os.makedirs(OUT, exist_ok=True)
    icon(180, a.font).save(os.path.join(OUT, "apple-touch-icon.png"), optimize=True)
    icon(192, a.font).save(os.path.join(OUT, "pwa-icon-192.png"), optimize=True)
    icon(512, a.font).save(os.path.join(OUT, "pwa-icon-512.png"), optimize=True)
    icon(32, a.font, opaque=False).save(os.path.join(OUT, "favicon-32.png"), optimize=True)
    icon(64, a.font, opaque=False).save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)])
    with open(os.path.join(OUT, "favicon.svg"), "w", encoding="utf-8", newline="\n") as f:
        f.write(favicon_svg())
    og_cover(a.font, reg).save(os.path.join(OUT, "og-cover.png"), optimize=True)
    print("wrote", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
