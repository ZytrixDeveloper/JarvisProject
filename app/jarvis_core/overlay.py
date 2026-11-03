"""On-screen effects drawn with native layered windows (per-pixel alpha):

* Border: glowing edges + status pill on every monitor while Jarvis acts. Click-through.
* Orb: a small floating, draggable arc reactor that lives on top of everything while the
  HUD is hidden. Double-click opens Jarvis, right-click makes him listen. While Jarvis
  works it glides to the monitor you're using and spins faster.

Both are hidden from screen captures so Jarvis's own screenshots never see them.
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
EX_LAYERED, EX_TRANSPARENT, EX_TOPMOST, EX_TOOL, EX_NOACTIVATE = 0x80000, 0x20, 0x8, 0x80, 0x8000000
WDA_EXCLUDEFROMCAPTURE = 0x11
ULW_ALPHA = 2
WM_NCHITTEST, WM_NCLBUTTONDBLCLK, WM_NCRBUTTONUP, WM_EXITSIZEMOVE, WM_MOUSEACTIVATE = 0x84, 0xA3, 0xA5, 0x232, 0x21
CYAN, AMBER = (41, 211, 255), (255, 181, 71)
ORB = 132  # orb window size in pixels

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
# Explicit handle types: without them ctypes passes 64-bit handles as 32-bit ints and overflows.
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                       ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p, wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)


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


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _font(size):
    for name in ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def monitors() -> list[tuple[int, int, int, int]]:
    """(left, top, width, height) of every monitor."""
    out = []

    def cb(hmon, hdc, rect, _):
        r = rect.contents
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return out or [(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))]


def active_work_area(hwnd=None):
    """Work area (left, top, right, bottom, without the taskbar) of the monitor showing hwnd
    (default: the foreground window)."""
    mon = user32.MonitorFromWindow(hwnd or user32.GetForegroundWindow(), 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


_SHELL_PROCS = ("explorer.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
                "lockapp.exe", "textinputhost.exe")
_fs_cache = {"t": 0.0, "value": False}


def fullscreen_app_active() -> bool:
    """True while a game or other fullscreen app covers its whole monitor (game mode). Cached 0.5 s."""
    import os
    import time as _t
    if _t.time() - _fs_cache["t"] < 0.5:
        return _fs_cache["value"]
    _fs_cache["t"] = _t.time()
    value = False
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():
            name = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            proc = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            exe = ""
            if proc:
                if kernel32.QueryFullProcessImageNameW(proc, 0, name, ctypes.byref(size)):
                    exe = name.value.lower().rsplit("\\", 1)[-1]
                kernel32.CloseHandle(proc)
            if exe not in _SHELL_PROCS:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
                user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
                m = info.rcMonitor
                value = r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
    _fs_cache["value"] = value
    return value


# ---------------------------------------------------------------- drawing

def render_border(w, h, caption: str) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for i, a in enumerate((70, 45, 25)):
        g.rectangle([i * 6, i * 6, w - 1 - i * 6, h - 1 - i * 6], outline=CYAN + (a,), width=6)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w - 1, h - 1], outline=CYAN + (230,), width=3)
    L = 70
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
        ImageDraw.Draw(pill).rounded_rectangle([px - 6, py - 6, px + pw + 6, py + ph + 6], radius=31, fill=CYAN + (90,))
        img.alpha_composite(pill.filter(ImageFilter.GaussianBlur(12)))
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=25, fill=(4, 16, 26, 235), outline=CYAN + (255,), width=2)
        cx, cy = px + 30, py + ph // 2
        d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=CYAN + (255,), width=3)
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(210, 248, 255, 255))
        d.text((px + 54, py + ph // 2), text, font=f, fill=(214, 242, 255, 255), anchor="lm")
    return img


def render_orb(angle: float, color, bright: bool) -> Image.Image:
    s = ORB * 2  # draw at 2x, downscale for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    c = s / 2
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c - s * .40, c - s * .40, c + s * .40, c + s * .40],
                                 fill=color + (150 if bright else 80,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * .08)))
    d = ImageDraw.Draw(img)
    d.ellipse([c - s * .38, c - s * .38, c + s * .38, c + s * .38], fill=(3, 10, 18, 235))
    for i in range(60):  # tick ring, slowly turning
        a = math.radians(i * 6 + angle * .3)
        r1, r2 = s * .36, s * (.33 if i % 5 else .31)
        d.line([(c + r1 * math.cos(a), c + r1 * math.sin(a)), (c + r2 * math.cos(a), c + r2 * math.sin(a))],
               fill=color + (220 if i % 5 == 0 else 110,), width=3 if i % 5 == 0 else 2)
    for i in range(3):  # outer arcs spinning one way
        start = angle + i * 120
        d.arc([c - s * .29, c - s * .29, c + s * .29, c + s * .29], start, start + 70, fill=color + (240,), width=7)
    for i in range(10):  # reactor coils spinning the other way
        start = -angle * .7 + i * 36
        d.arc([c - s * .22, c - s * .22, c + s * .22, c + s * .22], start, start + 24,
              fill=(150, 235, 255, 255) if bright else color + (200,), width=16)
    core = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    cd = ImageDraw.Draw(core)
    for k, a in ((0.15, 120), (0.11, 200), (0.08, 255)):
        cd.ellipse([c - s * k, c - s * k, c + s * k, c + s * k], fill=(200, 245, 255, a))
    img.alpha_composite(core.filter(ImageFilter.GaussianBlur(s * .02)))
    d.ellipse([c - s * .045, c - s * .045, c + s * .045, c + s * .045], fill=(255, 255, 255, 255))
    return img.resize((ORB, ORB), Image.LANCZOS)


def _premultiplied_bgra(img: Image.Image) -> bytes:
    rgba = np.asarray(img, dtype=np.uint16)
    alpha = rgba[..., 3]
    bgra = np.empty(rgba.shape, np.uint8)
    for dst, src in ((0, 2), (1, 1), (2, 0)):
        bgra[..., dst] = rgba[..., src] * alpha // 255
    bgra[..., 3] = alpha
    return bgra.tobytes()


# ---------------------------------------------------------------- windows

class Layered:
    """One layered window whose content is a premultiplied BGRA bitmap."""

    def __init__(self, cls, x, y, w, h, ex_style):
        self.x, self.y, self.w, self.h = x, y, w, h
        hinst = kernel32.GetModuleHandleW(None)
        self.hwnd = user32.CreateWindowExW(ex_style, cls, "Jarvis", WS_POPUP, x, y, w, h, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError()
        user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE)
        self.screen_dc = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self.bmp = None
        self.visible = False

    def set_image(self, data: bytes):
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, data, len(data))
        gdi32.SelectObject(self.dc, bmp)
        if self.bmp:
            gdi32.DeleteObject(self.bmp)
        self.bmp = bmp

    def blit(self, alpha=255, pos=None):
        if pos:
            self.x, self.y = pos
        size, src, dst = wintypes.SIZE(self.w, self.h), wintypes.POINT(0, 0), wintypes.POINT(int(self.x), int(self.y))
        blend = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha))), 1)
        user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(dst), ctypes.byref(size),
                                   self.dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)

    def show(self, on: bool):
        if on != self.visible:
            user32.ShowWindow(self.hwnd, 4 if on else 0)  # SW_SHOWNOACTIVATE / SW_HIDE
            self.visible = on


class Overlay:
    def __init__(self, settings=None, on_open=None, on_listen=None, hud_visible=None):
        self.settings = settings
        self.on_open = on_open or (lambda: None)
        self.on_listen = on_listen or (lambda: None)
        self.hud_visible = hud_visible or (lambda: True)
        self.state = "idle"
        self._q = queue.Queue()
        threading.Thread(target=self._run, name="overlay", daemon=True).start()

    # public, thread-safe
    def show(self, caption: str):
        self._q.put(("show", caption))

    def hide(self):
        self._q.put(("hide", ""))

    def set_state(self, state: str):
        self.state = state

    # ------------------------------------------------------------ window thread

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if self.orb and hwnd == self.orb.hwnd:
            if msg == WM_NCHITTEST:
                return 2  # HTCAPTION: dragging the orb moves the window
            if msg == WM_MOUSEACTIVATE:
                return 3  # MA_NOACTIVATE
            if msg == WM_NCLBUTTONDBLCLK:
                threading.Thread(target=self.on_open, daemon=True).start()
                return 0
            if msg == WM_NCRBUTTONUP:
                threading.Thread(target=self.on_listen, daemon=True).start()
                return 0
            if msg == WM_EXITSIZEMOVE:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                self.orb.x, self.orb.y = r.left, r.top
                self.home = [r.left, r.top]
                if self.settings:
                    self.settings.update({"orb_position": self.home})
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        self.orb = None
        # Real pixels for everything in this thread: monitors with different zoom levels otherwise
        # mix scaled and unscaled coordinates and the orb ends up off-screen.
        try:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except (AttributeError, OSError):
            pass
        try:
            self._proc = WNDPROC(self._wndproc)
            hinst = kernel32.GetModuleHandleW(None)
            user32.RegisterClassW(ctypes.byref(WNDCLASS(lpfnWndProc=self._proc, hInstance=hinst,
                                                        lpszClassName="JarvisLayer")))
            click_through = EX_LAYERED | EX_TRANSPARENT | EX_TOPMOST | EX_TOOL | EX_NOACTIVATE
            self.borders = [Layered("JarvisLayer", x, y, w, h, click_through) for x, y, w, h in monitors()]
            frames = {key: [_premultiplied_bgra(render_orb(a * 2.5, col, bright)) for a in range(48)]
                      for key, col, bright in (("idle", CYAN, False), ("busy", CYAN, True), ("confirm", AMBER, True))}
            self.orb = Layered("JarvisLayer", 0, 0, ORB, ORB, EX_LAYERED | EX_TOPMOST | EX_TOOL | EX_NOACTIVATE)
        except Exception:  # noqa: BLE001 - effects are cosmetic, never crash Jarvis
            log.exception("overlay unavailable")
            return
        saved = self.settings["orb_position"] if self.settings else []
        l, t, r, b = active_work_area()
        self.home = list(saved) if len(saved or []) == 2 else [r - ORB - 24, b - ORB - 24]
        on_screen = any(x <= self.home[0] + ORB / 2 < x + w and y <= self.home[1] + ORB / 2 < y + h
                        for x, y, w, h in monitors())
        if not on_screen:  # monitor unplugged or bad saved spot: back to the bottom-right corner
            self.home = [r - ORB - 24, b - ORB - 24]
        self.orb.x, self.orb.y = self.home
        border_on, caption, phase, start = False, "", 0.0, time.time()
        msg = wintypes.MSG()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                while True:
                    cmd, text = self._q.get_nowait()
                    if cmd == "show" and (text != caption or not border_on):
                        caption = text
                        for b_ in self.borders:
                            b_.set_image(_premultiplied_bgra(render_border(b_.w, b_.h, caption)))
                    border_on = cmd == "show"
            except queue.Empty:
                pass
            t = time.time() - start
            # Game mode: while a game / fullscreen app is in front, draw nothing on top of it.
            gaming = (self.settings is None or self.settings["game_mode"]) and fullscreen_app_active()
            glow = border_on and not gaming
            for b_ in self.borders:
                b_.show(glow)
                if glow:
                    b_.blit(170 + 85 * (0.5 + 0.5 * math.sin(t * 3.2)))
            if gaming:
                self.orb.show(False)
            else:
                self._animate_orb(frames, border_on, phase)
            busy = border_on or self.state in ("thinking", "transcribing", "scanning", "listening", "speaking")
            phase += 2.2 if busy else 0.45
            time.sleep(1 / 30 if (glow or self.orb.visible) else 0.1)  # idle: barely any CPU

    def _animate_orb(self, frames, working, phase):
        want = (self.settings is None or self.settings["show_orb"]) and (working or not self.hud_visible())
        if not want:
            self.orb.show(False)
            return
        key = "confirm" if self.state == "confirm" else "busy" if working or self.state != "idle" else "idle"
        self.orb.set_image(frames[key][int(phase) % 48])
        # glide to the top-right of the monitor you're using while working, then back home
        if working:
            l, t, r, b = active_work_area()
            target = [r - ORB - 40, t + 90]
        else:
            target = self.home
        x = self.orb.x + (target[0] - self.orb.x) * 0.15
        y = self.orb.y + (target[1] - self.orb.y) * 0.15
        pulse = 1.0 if key == "idle" else 0.85 + 0.15 * math.sin(phase * 0.4)
        self.orb.blit(255 * pulse, (x, y))
        self.orb.show(True)
