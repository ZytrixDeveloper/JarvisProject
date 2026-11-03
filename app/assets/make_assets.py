"""Generates the EXE icon and the installer artwork. Run: python assets/make_assets.py"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jarvis_core.icon import reactor_icon  # noqa: E402

HERE = Path(__file__).resolve().parent
BG = (3, 8, 14)
CYAN = (41, 211, 255)


def grid(img, step):
    d = ImageDraw.Draw(img)
    for x in range(0, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(8, 30, 44))
    for y in range(0, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(8, 30, 44))


def font(size):
    for name in ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wizard_image():
    # Inno Setup "modern" wizard side panel; drawn at 2x for high-DPI screens.
    w, h = 328, 628
    img = Image.new("RGB", (w, h), BG)
    grid(img, 24)
    icon = reactor_icon(240)
    img.paste(icon, ((w - 240) // 2, 150), icon)
    d = ImageDraw.Draw(img)
    title = "J.A.R.V.I.S."
    f = font(34)
    tw = d.textlength(title, font=f)
    d.text(((w - tw) / 2, 430), title, font=f, fill=CYAN)
    sub = "Personal AI Assistant"
    f2 = font(20)
    d.text(((w - d.textlength(sub, font=f2)) / 2, 478), sub, font=f2, fill=(120, 160, 180))
    d.rectangle([w // 2 - 30, 520, w // 2 + 30, 522], fill=CYAN)
    img.save(HERE / "wizard.bmp")


def wizard_small():
    img = Image.new("RGB", (110, 110), BG)
    icon = reactor_icon(96)
    img.paste(icon, (7, 7), icon)
    img.save(HERE / "wizard-small.bmp")


def version_info():
    """Windows version resource for Jarvis.exe (publisher, product, version). Unsigned EXEs without
    this metadata look more suspicious to antivirus heuristics."""
    from jarvis_core.config import APP_VERSION
    nums = tuple(int(n) for n in APP_VERSION.split("."))[:3] + (0,)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'ZytrixDeveloper'),
      StringStruct('FileDescription', 'J.A.R.V.I.S. personal AI assistant'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', 'Jarvis'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 ZytrixDeveloper. MIT License.'),
      StringStruct('OriginalFilename', 'Jarvis.exe'),
      StringStruct('ProductName', 'J.A.R.V.I.S.'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    (HERE / "version_info.txt").write_text(text, "utf-8")


if __name__ == "__main__":
    version_info()
    reactor_icon(256).save(HERE / "jarvis.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    reactor_icon(512).save(HERE / "jarvis.png")
    wizard_image()
    wizard_small()
    print("assets written to", HERE)
