"""Glowing "Jarvis is working" overlay, drawn over the whole screen while Jarvis acts.

A native layered window (per-pixel alpha via UpdateLayeredWindow): always on top,
click-through, never takes focus, hidden from the taskbar and from screen captures,
so Jarvis's own screenshots don't see it.
"""
import ctypes
import logging
import math
import queue
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("jarvis.overlay")
user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000  # layered|transparent|topmost|toolwindow|noactivate
WDA_EXCLUDEFROMCAPTURE = 0x11
ULW_ALPHA = 2
CYAN = (41, 211, 255)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _font(size):
    for name in ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render(w, h, caption: str) -> Image.Image:
    """Screen-sized RGBA frame: glowing edges, corner brackets and a status pill."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for i, a in enumerate((70, 45, 25)):
        g.rectangle([i * 6, i * 6, w - 1 - i * 6, h - 1 - i * 6], outline=CYAN + (a,), width=6)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=CYAN + (230,), width=3)
    L = 70  # corner brackets
    for x, y, dx, dy in ((8, 8, 1, 1), (w - 9, 8, -1, 1), (8, h - 9, 1, -1), (w - 9, h - 9, -1, -1)):
        d.line([(x, y), (x + L * dx, y)], fill=(160, 240, 255, 255), width=4)
        d.line([(x, y), (x, y + L * dy)], fill=(160, 240, 255, 255), width=4)
    if caption:
        f = _font(22)
        text = caption if len(caption) < 70 else caption[:67] + "..."
        tw = int(d.textlength(text, font=f))
        pw, ph = tw + 92, 50
        px, py = (w - pw) // 2, 26
        pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        pd.rounded_rectangle([px - 6, py - 6, px + pw + 6, py + ph + 6], radius=31, fill=CYAN + (90,))
        img.alpha_composite(pill.filter(ImageFilter.GaussianBlur(12)))
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=25, fill=(4, 16, 26, 235), outline=CYAN + (255,), width=2)
        cx, cy = px + 30, py + ph // 2
        d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=CYAN + (255,), width=3)
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(210, 248, 255, 255))
        d.text((px + 54, py + ph // 2), text, font=f, fill=(214, 242, 255, 255), anchor="lm")
    return img


class Overlay:
    def __init__(self):
        self._q = queue.Queue()
        self._caption = ""
        self._visible = False
        self.enabled = True
        threading.Thread(target=self._run, name="overlay", daemon=True).start()

    def show(self, caption: str):
        if self.enabled:
            self._q.put(("show", caption))

    def hide(self):
        self._q.put(("hide", ""))

    # ------------------------------------------------------------ window thread

    def _run(self):
        try:
            self._create()
        except Exception:  # noqa: BLE001 - the overlay is cosmetic, never crash Jarvis
            log.exception("overlay unavailable")
            return
        msg = wintypes.MSG()
        start = time.time()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                while True:
                    cmd, caption = self._q.get_nowait()
                    if cmd == "show":
                        if caption != self._caption or not self._visible:
                            self._caption = caption
                            self._upload(_render(self.w, self.h, caption))
                        if not self._visible:
                            user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
                            self._visible = True
                    elif self._visible:
                        user32.ShowWindow(self.hwnd, 0)
                        self._visible = False
            except queue.Empty:
                pass
            if self._visible:  # breathing glow
                self._blit(int(170 + 85 * (0.5 + 0.5 * math.sin((time.time() - start) * 3.2))))
                time.sleep(1 / 30)
            else:
                time.sleep(0.08)

    def _create(self):
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="JarvisOverlay")
        user32.RegisterClassW(ctypes.byref(wc))
        self.x = self.y = 0
        self.w, self.h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)  # primary monitor
        user32.CreateWindowExW.restype = wintypes.HWND
        self.hwnd = user32.CreateWindowExW(WS_EX, "JarvisOverlay", "Jarvis overlay", WS_POPUP,
                                           self.x, self.y, self.w, self.h, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        self._screen_dc = user32.GetDC(None)
        self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
        self._bmp = None

    def _upload(self, img: Image.Image):
        # UpdateLayeredWindow wants premultiplied BGRA in a top-down DIB.
        rgba = np.asarray(img, dtype=np.uint16)
        alpha = rgba[..., 3]
        bgra = np.empty(rgba.shape, np.uint8)
        for dst, src in ((0, 2), (1, 1), (2, 0)):
            bgra[..., dst] = rgba[..., src] * alpha // 255
        bgra[..., 3] = alpha
        data = bgra.tobytes()
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        bmp = gdi32.CreateDIBSection(self._mem_dc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, data, len(data))
        gdi32.SelectObject(self._mem_dc, bmp)
        if self._bmp:
            gdi32.DeleteObject(self._bmp)
        self._bmp = bmp

    def _blit(self, alpha: int):
        size = wintypes.SIZE(self.w, self.h)
        src = wintypes.POINT(0, 0)
        dst = wintypes.POINT(self.x, self.y)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, alpha)), 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        user32.UpdateLayeredWindow(self.hwnd, self._screen_dc, ctypes.byref(dst), ctypes.byref(size),
                                   self._mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
