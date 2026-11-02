"""Draws the arc-reactor icon used for the tray and the EXE."""
from PIL import Image, ImageDraw, ImageFilter


def reactor_icon(size: int = 256) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((s * .08, s * .08, s * .92, s * .92), outline=(41, 211, 255, 255), width=max(2, s // 14))
    g.ellipse((s * .33, s * .33, s * .67, s * .67), fill=(160, 240, 255, 255))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s / 28)))
    d = ImageDraw.Draw(img)
    d.ellipse((s * .02, s * .02, s * .98, s * .98), fill=(4, 14, 24, 255))
    d.ellipse((s * .1, s * .1, s * .9, s * .9), outline=(41, 211, 255, 255), width=max(2, s // 18))
    for i in range(10):  # reactor segments
        start = i * 36 + 4
        d.arc((s * .2, s * .2, s * .8, s * .8), start, start + 26, fill=(120, 225, 255, 255), width=max(2, s // 12))
    d.ellipse((s * .36, s * .36, s * .64, s * .64), fill=(210, 248, 255, 255))
    return img


if __name__ == "__main__":
    import sys
    reactor_icon(256).save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
