"""Dev helper: save a screenshot of only the Jarvis window (even if other windows cover it).
Usage: python tools/snap_window.py out.png ["Window title"]"""
import ctypes
import sys
from ctypes import wintypes

from PIL import Image

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
user32.SetProcessDPIAware()
hwnd = user32.FindWindowW(None, sys.argv[2] if len(sys.argv) > 2 else "J.A.R.V.I.S.")
if not hwnd:
    sys.exit("Jarvis window not found")
rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
w, h = rect.right - rect.left, rect.bottom - rect.top
hdc = user32.GetWindowDC(hwnd)
mem = gdi32.CreateCompatibleDC(hdc)
bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
gdi32.SelectObject(mem, bmp)
user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT: works for WebView2 content


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
buf = ctypes.create_string_buffer(w * h * 4)
gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).save(sys.argv[1])
gdi32.DeleteObject(bmp)
gdi32.DeleteDC(mem)
user32.ReleaseDC(hwnd, hdc)
print("saved", w, h)
