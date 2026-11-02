"""Everything Jarvis can do on the PC.

Every method marked with @tool becomes a function the AI can call. Its docstring
and type hints are turned into the JSON schema the model sees, so keep them short
and clear. Risky tools ask the user through ctx.confirm() before doing anything.
"""
import ctypes
import datetime as dt
import difflib
import fnmatch
import inspect
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser
import winreg
from ctypes import wintypes
from pathlib import Path

import psutil
import requests

from .config import SCREENSHOT_DIR

log = logging.getLogger("jarvis.tools")

NO_WINDOW = 0x08000000
user32 = ctypes.windll.user32
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Jarvis/0.1"}


class ToolError(Exception):
    """A failure the model should hear about in plain words."""


def tool(**enums):
    def mark(fn):
        fn._is_tool = True
        fn._enums = enums
        return fn
    return mark


# ---------------------------------------------------------------- schema helpers

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema(fn) -> dict:
    doc = inspect.getdoc(fn) or ""
    summary, _, args_doc = doc.partition("Args:")
    arg_help = {}
    for line in args_doc.splitlines():
        m = re.match(r"\s*(\w+):\s*(.+)", line)
        if m:
            arg_help[m.group(1)] = m.group(2).strip()
    props, required = {}, []
    for p in inspect.signature(fn).parameters.values():
        prop = {"type": _JSON_TYPES.get(p.annotation, "string"), "description": arg_help.get(p.name, "")}
        if p.name in fn._enums:
            prop["enum"] = fn._enums[p.name]
        props[p.name] = prop
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": " ".join(summary.split()),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _coerce(value, annotation):
    if annotation is int:
        return int(float(value))
    if annotation is float:
        return float(value)
    if annotation is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes")
    return value if isinstance(value, str) else json.dumps(value) if isinstance(value, (dict, list)) else str(value)


# ---------------------------------------------------------------- windows helpers

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WM_CLOSE = 0x0010


def _is_cloaked(hwnd) -> bool:
    cloaked = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except OSError:
        return False
    return bool(cloaked.value)


def visible_windows():
    """[(hwnd, title, pid)] of real, visible top-level windows."""
    out = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _is_cloaked(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if buf.value not in ("Program Manager", "Windows Input Experience"):
                    out.append((hwnd, buf.value, pid.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _proc_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower().removesuffix(".exe")
    except (psutil.Error, OSError):
        return ""


class FocusTracker(threading.Thread):
    """Remembers the last window the user used that isn't Jarvis, so typing goes there."""

    def __init__(self):
        super().__init__(daemon=True)
        self.last = None

    def run(self):
        me = os.getpid()
        while True:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != me:
                    self.last = hwnd
            time.sleep(0.4)

    def restore(self):
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid() and self.last and user32.IsWindow(self.last):
            if user32.IsIconic(self.last):
                user32.ShowWindow(self.last, 9)  # SW_RESTORE
            user32.SetForegroundWindow(self.last)
            time.sleep(0.3)


_SHELL_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Personal",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "My Pictures",
    "music": "My Music",
    "videos": "My Video",
}


def known_folder(name: str) -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            value, _ = winreg.QueryValueEx(key, _SHELL_FOLDERS[name])
            return Path(os.path.expandvars(value))
    except OSError:
        return Path.home() / name.capitalize()


def resolve_path(raw: str) -> Path:
    """Turn 'desktop/notes.txt', '~/x', '%USERPROFILE%\\y' or 'notes.txt' into a real path."""
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip('"').strip("'")))
    path = Path(text)
    if path.is_absolute():
        return path
    first = path.parts[0].lower() if path.parts else ""
    if first in _SHELL_FOLDERS:
        return known_folder(first).joinpath(*path.parts[1:])
    return known_folder("desktop") / path


_NEVER_TOUCH = [Path(os.environ.get("SystemRoot", r"C:\Windows")),
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                Path(os.environ.get("ProgramData", r"C:\ProgramData"))]


def _check_safe_target(path: Path):
    p = path.resolve()
    if len(p.parts) <= 2 or p == Path.home().resolve():
        raise ToolError(f"{p} is too important to touch.")
    if any(p == known_folder(k).resolve() for k in _SHELL_FOLDERS):
        raise ToolError(f"{p} is a main user folder, I won't move or delete it.")
    for root in _NEVER_TOUCH:
        if p == root or root in p.parents:
            raise ToolError(f"{p} is a system location, I won't change it.")


# ---------------------------------------------------------------- app name tables

APP_ALIASES = {
    "chrome": "google chrome", "edge": "microsoft edge", "vscode": "visual studio code",
    "vs code": "visual studio code", "code": "visual studio code", "files": "file explorer",
    "explorer": "file explorer", "cmd": "command prompt", "calc": "calculator",
    "word": "word", "excel": "excel", "powerpoint": "powerpoint",
}

PROCESS_NAMES = {
    "google chrome": ["chrome"], "chrome": ["chrome"], "microsoft edge": ["msedge"], "edge": ["msedge"],
    "firefox": ["firefox"], "word": ["winword"], "excel": ["excel"], "powerpoint": ["powerpnt"],
    "visual studio code": ["code"], "vs code": ["code"], "vscode": ["code"], "teams": ["ms-teams", "teams"],
    "spotify": ["spotify"], "discord": ["discord"], "steam": ["steam", "steamwebhelper"],
    "file explorer": ["explorer"], "explorer": ["explorer"], "notepad": ["notepad"],
    "calculator": ["calculatorapp"], "settings": ["systemsettings"], "task manager": ["taskmgr"],
    "obs": ["obs64"], "whatsapp": ["whatsapp", "whatsapp.root"], "telegram": ["telegram"],
}

# Built-in Windows apps by AppID, so they are found whatever language Windows is in.
BUILTIN_APP_IDS = {
    "calculator": "microsoft.windowscalculator", "notepad": "microsoft.windowsnotepad",
    "paint": "microsoft.paint", "camera": "microsoft.windowscamera", "photos": "microsoft.windows.photos",
    "clock": "microsoft.windowsalarms", "alarms": "microsoft.windowsalarms",
    "snipping tool": "microsoft.screensketch", "terminal": "microsoft.windowsterminal",
    "control panel": "microsoft.windows.controlpanel", "microsoft store": "microsoft.windowsstore",
    "store": "microsoft.windowsstore", "file explorer": "microsoft.windows.explorer",
    "xbox": "microsoft.gamingapp", "edge": "msedge", "microsoft edge": "msedge",
}
EXE_FALLBACK = {
    "task manager": "taskmgr.exe", "command prompt": "cmd.exe", "powershell": "powershell.exe",
    "file explorer": "explorer.exe", "notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe",
}

SETTINGS_PAGES = {
    "settings": "ms-settings:", "wifi": "ms-settings:network-wifi", "wi-fi": "ms-settings:network-wifi",
    "bluetooth": "ms-settings:bluetooth", "display": "ms-settings:display", "sound": "ms-settings:sound",
    "windows update": "ms-settings:windowsupdate", "update": "ms-settings:windowsupdate",
    "apps": "ms-settings:appsfeatures", "battery": "ms-settings:batterysaver", "privacy": "ms-settings:privacy",
    "night light": "ms-settings:nightlight", "background": "ms-settings:personalization-background",
}

# Never closed, whatever the user says (closing these breaks Windows or Jarvis itself).
CRITICAL_PROCESSES = {
    "system", "csrss", "wininit", "winlogon", "services", "lsass", "smss", "svchost", "dwm",
    "fontdrvhost", "sihost", "ctfmon", "searchhost", "startmenuexperiencehost",
    "shellexperiencehost", "runtimebroker", "audiodg", "ollama", "ollama app", "msedgewebview2",
}

DANGEROUS_COMMANDS = re.compile(
    r"format\s+[a-z]:|diskpart|bcdedit|vssadmin\s+delete|cipher\s+/w|reg\s+delete\s+hklm|"
    r"remove-item\s+['\"]?[a-z]:\\?['\"]?\s|rd\s+/s\s+/q\s+[a-z]:\\?\s*$|del\s+/[sfq].*[a-z]:\\\*",
    re.I,
)

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def _best_match(query: str, names: list[str]):
    """Index of the name that best matches query, or None."""
    q = query.lower().strip()
    lowered = [n.lower() for n in names]
    if q in lowered:
        return lowered.index(q)
    for test in (lambda n: n.startswith(q), lambda n: q in n,
                 lambda n: all(w in n for w in q.split())):
        hits = [i for i, n in enumerate(lowered) if test(n)]
        if hits:
            return min(hits, key=lambda i: len(lowered[i]))
    close = difflib.get_close_matches(q, lowered, n=1, cutoff=0.75)
    return lowered.index(close[0]) if close else None


def _jpeg(image, max_side=1280) -> bytes:
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=82)
    return buf.getvalue()


# ---------------------------------------------------------------- the toolbox

class Toolbox:
    def __init__(self, settings, ctx):
        self.settings = settings
        self.ctx = ctx
        self.focus = FocusTracker()
        self.focus.start()
        self._apps = None
        self._tools = {name: getattr(self, name) for name in dir(self)
                       if getattr(getattr(self, name), "_is_tool", False)}

    def schemas(self) -> list:
        return [_schema(fn) for fn in self._tools.values()]

    def run(self, name: str, args: dict) -> str:
        fn = self._tools.get(name)
        if fn is None:
            return f"Error: there is no tool called {name}."
        params = inspect.signature(fn).parameters
        try:
            clean = {k: _coerce(v, params[k].annotation) for k, v in (args or {}).items() if k in params}
            result = fn(**clean)
        except ToolError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - any failure is reported back to the model
            log.exception("tool %s failed", name)
            return f"Error: {type(e).__name__}: {e}"
        return str(result)[:3000]

    def _protected(self, *names: str) -> str | None:
        for keyword in self.settings["protected_apps"]:
            k = keyword.lower().strip()
            if k and any(k in n.lower() for n in names if n):
                return keyword
        return None

    # ------------------------------------------------------------ apps & windows

    def _start_apps(self, refresh=False):
        if self._apps is None or refresh:
            cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                   "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                                 encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
            data = json.loads(out.stdout or "[]")
            data = [data] if isinstance(data, dict) else data
            self._apps = [(a["Name"], a["AppID"]) for a in data if a.get("Name")]
        return self._apps

    @tool()
    def open_app(self, name: str) -> str:
        """Open an installed application, game or Windows settings page by name.

        Args:
            name: app name as the user said it, e.g. "chrome", "spotify", "bluetooth settings"
        """
        q = name.lower().strip().removesuffix(" app")
        page = q.removesuffix(" settings").strip()
        if q.endswith("settings") and page in SETTINGS_PAGES or q == "settings":
            os.startfile(SETTINGS_PAGES.get(page, "ms-settings:"))
            return f"Opened {name}."
        q = APP_ALIASES.get(q, q)
        for refresh in (False, True):
            apps = self._start_apps(refresh)
            builtin = BUILTIN_APP_IDS.get(q)
            hit = next((a for a in apps if builtin and builtin in a[1].lower()), None)
            if hit is None:
                i = _best_match(q, [a[0] for a in apps])
                hit = apps[i] if i is not None else None
            if hit:
                app_name, app_id = hit
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=NO_WINDOW)
                return f"Opened {app_name}."
        exe = shutil.which(EXE_FALLBACK.get(q, q)) or shutil.which(q + ".exe")
        if exe:
            subprocess.Popen([exe], creationflags=NO_WINDOW)
            return f"Started {exe}."
        guesses = difflib.get_close_matches(q, [a[0].lower() for a in self._apps], n=3, cutoff=0.4)
        raise ToolError(f"No app called '{name}' found." + (f" Similar: {', '.join(guesses)}." if guesses else ""))

    @tool()
    def close_app(self, name: str) -> str:
        """Close an application's windows (politely, so it can still ask to save work).

        Args:
            name: app or window name, e.g. "chrome", "notepad", "spotify"
        """
        q = APP_ALIASES.get(name.lower().strip(), name.lower().strip())
        targets = PROCESS_NAMES.get(q, [q.replace(" ", "")])
        me = os.getpid()
        windows, names = [], set()
        for hwnd, title, pid in visible_windows():
            proc = _proc_name(pid)
            if pid == me or proc in CRITICAL_PROCESSES:
                continue
            if any(t == proc or t in proc for t in targets) or q in title.lower():
                windows.append((hwnd, title, proc))
                names.add(proc)
        guard = self._protected(q, *names, *(w[1] for w in windows))
        if guard:
            raise ToolError(f"{guard} is on the protected list, so I will not close it.")
        if windows:
            for hwnd, _, _ in windows:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return f"Closed {len(windows)} window(s) of {', '.join(sorted(names))}."
        running = {p.info["name"] for p in psutil.process_iter(["name"])
                   if p.info["name"] and any(t == p.info["name"].lower().removesuffix(".exe") for t in targets)}
        if running and not any(n.lower().removesuffix(".exe") in CRITICAL_PROCESSES for n in running):
            for exe in running:
                subprocess.run(["taskkill", "/IM", exe], capture_output=True, creationflags=NO_WINDOW)
            return f"Asked {', '.join(running)} to close."
        raise ToolError(f"'{name}' doesn't seem to be open.")

    @tool()
    def list_open_windows(self) -> str:
        """List the windows/apps that are currently open."""
        me = os.getpid()
        rows = [f"{title} [{_proc_name(pid)}]" for _, title, pid in visible_windows() if pid != me]
        return "\n".join(rows[:30]) or "No open windows."

    # ------------------------------------------------------------ web

    @tool()
    def open_website(self, url: str) -> str:
        """Open a website in the default browser.

        Args:
            url: full URL or a site name like "youtube.com" or "netflix"
        """
        u = url.strip()
        if not re.match(r"^[a-z]+://", u, re.I):
            if "." not in u and " " not in u:
                u = f"www.{u}.com"
            u = "https://" + u
        webbrowser.open(u)
        return f"Opened {u}."

    @tool()
    def google_search(self, query: str) -> str:
        """Open a Google search in the browser so the user can look at the results.

        Args:
            query: what to search for
        """
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
        return f"Opened Google results for '{query}'."

    @tool()
    def search_web(self, query: str) -> str:
        """Search the internet and return results as text for you to read and answer from.
        Use for news, facts you are unsure about, prices, scores, anything recent.

        Args:
            query: search query
        """
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        results = DDGS().text(query, max_results=5) or []
        if not results:
            raise ToolError("No results found.")
        return "\n".join(f"{i}. {r.get('title')}: {r.get('body')} ({r.get('href')})"
                         for i, r in enumerate(results, 1))

    @tool()
    def get_news(self, topic: str = "", count: int = 5) -> str:
        """Get the latest news headlines, optionally about a topic. Also shows them in the HUD news panel.

        Args:
            topic: e.g. "technology", "football", "Slovakia"; empty for top world news
            count: how many headlines (1-10)
        """
        items = fetch_news(topic, max(1, min(10, count)))
        if not items:
            raise ToolError("I couldn't get any news right now.")
        self.ctx.show_news(topic, items)
        return "\n".join(f"- {n['title']} ({n['source']}, {n['age']})" for n in items)

    @tool()
    def read_clipboard(self) -> str:
        """Read the text the user has copied (Ctrl+C), e.g. to summarise, translate or explain it."""
        import pyperclip
        text = pyperclip.paste()
        if not text.strip():
            raise ToolError("The clipboard is empty or doesn't contain text.")
        return text[:4000]

    @tool()
    def play_youtube(self, query: str) -> str:
        """Find a video or song on YouTube and play it in the browser.

        Args:
            query: song or video to play
        """
        search = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        try:
            html = requests.get(search, headers=HTTP_HEADERS, timeout=8).text
            m = re.search(r'"videoId":"([\w-]{11})"', html)
        except requests.RequestException:
            m = None
        webbrowser.open(f"https://www.youtube.com/watch?v={m.group(1)}" if m else search)
        return f"Playing '{query}' on YouTube." if m else f"Opened YouTube search for '{query}'."

    # ------------------------------------------------------------ system

    @tool()
    def system_status(self) -> str:
        """Get CPU, memory, disk, battery and uptime of this PC."""
        s = stats()
        parts = [f"CPU {s['cpu']}%", f"RAM {s['ram']}% of {s['ram_total']} GB", f"disk C: {s['disk']}% full"]
        if s["battery"] is not None:
            parts.append(f"battery {s['battery']}% ({'charging' if s['plugged'] else 'on battery'})")
        if s.get("gpu") is not None:
            parts.append(f"GPU {s['gpu']}%, VRAM {s['vram']}")
        parts.append(f"uptime {s['uptime']}")
        return ", ".join(parts)

    @tool()
    def get_weather(self, city: str = "") -> str:
        """Current weather and today's forecast.

        Args:
            city: city name; leave empty for the user's home city / current location
        """
        city = city.strip() or self.settings["home_city"]
        lat = lon = None
        label = city or "your location"
        try:
            lat, lon, label = self._locate(city)
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("geocoding failed: %s", e)
        if lat is not None:
            try:
                return self._open_meteo(lat, lon, label)
            except (requests.RequestException, KeyError, ValueError) as e:
                log.warning("open-meteo failed (%s), trying wttr.in", e)
        where = f"{lat},{lon}" if lat is not None else city
        w = requests.get(f"https://wttr.in/{urllib.parse.quote(where)}", params={"format": "j1"},
                         headers=HTTP_HEADERS, timeout=10).json()
        c, d = w["current_condition"][0], w["weather"][0]
        return (f"{label}: {c['weatherDesc'][0]['value'].strip().lower()}, {c['temp_C']}°C "
                f"(feels {c['FeelsLikeC']}°C), humidity {c['humidity']}%, wind {c['windspeedKmph']} km/h. "
                f"Today {d['mintempC']}–{d['maxtempC']}°C.")

    def _locate(self, city: str):
        if not city:
            ip = requests.get("https://ipwho.is/", timeout=8).json()
            return ip["latitude"], ip["longitude"], ip.get("city", "your location")
        geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                           params={"name": city, "count": 1}, timeout=8)
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            raise ToolError(f"I couldn't find a place called {city}.")
        return results[0]["latitude"], results[0]["longitude"], results[0]["name"]

    def _open_meteo(self, lat: float, lon: float, label: str) -> str:
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=8, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        })
        r.raise_for_status()
        c, d = r.json()["current"], r.json()["daily"]
        return (f"{label}: {WEATHER_CODES.get(c['weather_code'], 'unknown')}, {c['temperature_2m']}°C "
                f"(feels {c['apparent_temperature']}°C), humidity {c['relative_humidity_2m']}%, "
                f"wind {c['wind_speed_10m']} km/h. Today {d['temperature_2m_min'][0]}–{d['temperature_2m_max'][0]}°C, "
                f"rain chance {d['precipitation_probability_max'][0]}%.")

    def _endpoint_volume(self):
        from comtypes import CLSCTX_ALL, CoInitialize
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        try:
            CoInitialize()
        except OSError:
            pass
        speakers = AudioUtilities.GetSpeakers()
        vol = getattr(speakers, "EndpointVolume", None)
        if vol is None:
            iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))
        return vol

    @tool(action=["set", "up", "down", "mute", "unmute"])
    def volume(self, action: str, amount: int = 10) -> str:
        """Change the PC's sound volume.

        Args:
            action: set, up, down, mute or unmute
            amount: for set: the level 0-100; for up/down: how many percent to change
        """
        try:
            vol = self._endpoint_volume()
            now = round(vol.GetMasterVolumeLevelScalar() * 100)
            if action in ("mute", "unmute"):
                vol.SetMute(1 if action == "mute" else 0, None)
                return f"Sound {action}d."
            target = amount if action == "set" else now + (amount if action == "up" else -amount)
            target = max(0, min(100, target))
            vol.SetMute(0, None)
            vol.SetMasterVolumeLevelScalar(target / 100, None)
            return f"Volume set to {target}%."
        except Exception:  # noqa: BLE001 - fall back to media keys if the audio API fails
            log.exception("pycaw failed, using media keys")
            import pyautogui
            key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute"}.get(action)
            if key is None:
                raise ToolError("I can only nudge the volume up or down right now.")
            pyautogui.press(key, presses=1 if "mute" in action else max(1, amount // 2))
            return f"Volume {action}."

    @tool(action=["play_pause", "next", "previous", "stop"])
    def media_control(self, action: str) -> str:
        """Control music or video that is playing (Spotify, YouTube, etc.).

        Args:
            action: play_pause, next, previous or stop
        """
        import pyautogui
        pyautogui.press({"play_pause": "playpause", "next": "nexttrack",
                         "previous": "prevtrack", "stop": "stop"}[action])
        return f"Media: {action.replace('_', '/')}."

    @tool()
    def set_brightness(self, level: int) -> str:
        """Set the screen brightness.

        Args:
            level: 0 to 100
        """
        import screen_brightness_control as sbc
        sbc.set_brightness(max(0, min(100, level)))
        return f"Brightness set to {level}%."

    @tool(action=["lock", "sleep", "shutdown", "restart", "cancel"])
    def power(self, action: str) -> str:
        """Lock, sleep, shut down or restart the PC, or cancel a pending shutdown.

        Args:
            action: lock, sleep, shutdown, restart or cancel
        """
        if action == "lock":
            user32.LockWorkStation()
            return "PC locked."
        if action == "cancel":
            subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
            return "Pending shutdown cancelled."
        if not self.ctx.confirm(f"{action.capitalize()} the PC?"):
            return "The user cancelled."
        if action == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NO_WINDOW)
            return "Going to sleep."
        flag = "/s" if action == "shutdown" else "/r"
        subprocess.run(["shutdown", flag, "/t", "30"], capture_output=True, creationflags=NO_WINDOW)
        return f"{action.capitalize()} in 30 seconds. Say 'cancel shutdown' to stop it."

    @tool()
    def set_timer(self, minutes: float, label: str = "") -> str:
        """Start a countdown timer; Jarvis announces when it's done.

        Args:
            minutes: length in minutes (0.5 = 30 seconds)
            label: what the timer is for, e.g. "pizza"
        """
        what = label or "timer"
        t = threading.Timer(minutes * 60, lambda: self.ctx.announce(
            f"{self.settings['address_as'].capitalize()}, your {what} timer is done."))
        t.daemon = True
        t.start()
        return f"Timer set for {minutes:g} minute(s)."

    # ------------------------------------------------------------ keyboard

    @tool()
    def type_text(self, text: str) -> str:
        """Type text into the app the user is using (where the cursor is).

        Args:
            text: the exact text to type
        """
        import pyautogui
        import pyperclip
        self.focus.restore()
        try:
            old = pyperclip.paste()
        except pyperclip.PyperclipException:
            old = None
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        if old is not None:
            pyperclip.copy(old)
        return f"Typed {len(text)} characters."

    @tool()
    def press_keys(self, keys: str) -> str:
        """Press a key or keyboard shortcut in the app the user is using.

        Args:
            keys: e.g. "enter", "ctrl+s", "alt+tab", "win+d", "ctrl+shift+esc"
        """
        import pyautogui
        names = {"win": "winleft", "windows": "winleft", "control": "ctrl", "escape": "esc", "return": "enter"}
        combo = [names.get(k.strip().lower(), k.strip().lower()) for k in keys.split("+") if k.strip()]
        self.focus.restore()
        pyautogui.hotkey(*combo)
        return f"Pressed {keys}."

    # ------------------------------------------------------------ files

    @tool()
    def find_files(self, name: str, folder: str = "") -> str:
        """Search the user's folders (Desktop, Documents, Downloads, Pictures, Music, Videos) for files.

        Args:
            name: part of the file name, or a pattern like "*.pdf"
            folder: optional folder to search instead, e.g. "downloads"
        """
        roots = [resolve_path(folder)] if folder else [known_folder(k) for k in _SHELL_FOLDERS]
        pattern = name.lower()
        wildcard = any(c in pattern for c in "*?")
        skip = {"node_modules", ".git", ".venv", "__pycache__", "appdata", "$recycle.bin"}
        found, deadline = [], time.time() + 6
        for root in dict.fromkeys(roots):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
                for f in filenames + dirnames:
                    fl = f.lower()
                    if fnmatch.fnmatch(fl, pattern) if wildcard else pattern in fl:
                        found.append(os.path.join(dirpath, f))
                if len(found) >= 20 or time.time() > deadline:
                    break
            if len(found) >= 20 or time.time() > deadline:
                break
        return "\n".join(found[:20]) if found else f"No files matching '{name}' found."

    @tool()
    def open_path(self, path: str) -> str:
        """Open a file with its default program, or open a folder in File Explorer.

        Args:
            path: full path, or e.g. "downloads", "desktop/report.pdf"
        """
        p = resolve_path(path)
        if not p.exists():
            raise ToolError(f"{p} does not exist.")
        os.startfile(p)
        return f"Opened {p}."

    @tool()
    def read_file(self, path: str) -> str:
        """Read a text file (notes, code, txt, csv...) so you can summarise or answer about it.

        Args:
            path: path to the file
        """
        p = resolve_path(path)
        if not p.is_file():
            raise ToolError(f"{p} is not a file.")
        return p.read_text("utf-8", errors="replace")[:4000]

    @tool()
    def write_file(self, path: str, content: str) -> str:
        """Create a text file with the given content. Relative paths go on the Desktop.

        Args:
            path: e.g. "shopping list.txt" or "documents/notes.md"
            content: the text to put in the file
        """
        p = resolve_path(path)
        if p.exists() and not self.ctx.confirm(f"Overwrite the existing file {p.name}?"):
            return "The user cancelled."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"Saved {p}."

    @tool()
    def create_folder(self, path: str) -> str:
        """Create a folder. Relative paths go on the Desktop.

        Args:
            path: e.g. "school projects" or "documents/invoices"
        """
        p = resolve_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"Created folder {p}."

    @tool()
    def move_file(self, source: str, destination: str) -> str:
        """Move or rename a file or folder.

        Args:
            source: what to move
            destination: new folder or new full path
        """
        src, dst = resolve_path(source), resolve_path(destination)
        if not src.exists():
            raise ToolError(f"{src} does not exist.")
        _check_safe_target(src)
        if not self.ctx.confirm(f"Move {src.name} to {dst}?"):
            return "The user cancelled."
        moved = shutil.move(str(src), str(dst))
        return f"Moved to {moved}."

    @tool()
    def delete_file(self, path: str) -> str:
        """Delete a file or folder by sending it to the Recycle Bin.

        Args:
            path: what to delete
        """
        from send2trash import send2trash
        p = resolve_path(path)
        if not p.exists():
            raise ToolError(f"{p} does not exist.")
        _check_safe_target(p)
        if not self.ctx.confirm(f"Send {p} to the Recycle Bin?"):
            return "The user cancelled."
        send2trash(str(p))
        return f"Moved {p.name} to the Recycle Bin."

    @tool()
    def run_command(self, command: str) -> str:
        """Run a PowerShell command and return its output. Only when no other tool can do the job.

        Args:
            command: the PowerShell command
        """
        if DANGEROUS_COMMANDS.search(command):
            raise ToolError("That command could damage the system, so it's blocked.")
        if not self.ctx.confirm(f"Run this command?\n{command}"):
            return "The user cancelled."
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=90,
                             creationflags=NO_WINDOW)
        text = (out.stdout + ("\n" + out.stderr if out.stderr else "")).strip()
        return f"exit code {out.returncode}\n{text[:2500] or '(no output)'}"

    # ------------------------------------------------------------ vision

    def _grab_screen(self):
        import mss
        from PIL import Image
        hidden = self.ctx.hide_hud()
        try:
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
        finally:
            if hidden:
                self.ctx.show_hud()
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    @tool()
    def look_at_screen(self, question: str = "Describe what is on the screen.") -> str:
        """Look at the user's screen and answer a question about it (read errors, describe, help).

        Args:
            question: what to find out from the screen
        """
        jpeg = _jpeg(self._grab_screen())
        self.ctx.capture("screen", jpeg)
        return self.ctx.vision(question, jpeg)

    @tool()
    def look_through_camera(self, question: str = "Describe what you see.") -> str:
        """Look through the webcam and answer a question (identify objects, read text, describe the user).

        Args:
            question: what to find out from the camera image
        """
        import cv2
        from PIL import Image
        cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        try:
            if not cam.isOpened():
                raise ToolError("No webcam found.")
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            frame = None
            for _ in range(12):  # let auto-exposure settle
                ok, f = cam.read()
                if ok:
                    frame = f
            if frame is None:
                raise ToolError("The webcam didn't return an image.")
        finally:
            cam.release()
        jpeg = _jpeg(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        self.ctx.capture("camera", jpeg)
        return self.ctx.vision(question, jpeg)

    @tool()
    def take_screenshot(self) -> str:
        """Save a screenshot of the screen to Pictures/Jarvis."""
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        image = self._grab_screen()
        image.save(path)
        self.ctx.capture("screen", _jpeg(image, 640))
        return f"Screenshot saved to {path}."


# ---------------------------------------------------------------- news

def _age(published: str) -> str:
    from email.utils import parsedate_to_datetime
    try:
        delta = dt.datetime.now(dt.timezone.utc) - parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return "recently"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(1, minutes)} min ago"
    if minutes < 1440:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def fetch_news(topic: str = "", count: int = 8) -> list[dict]:
    """Headlines from Google News RSS (BBC as backup). No API key needed."""
    import xml.etree.ElementTree as ET
    base = "https://news.google.com/rss"
    url = (f"{base}/search?q={urllib.parse.quote_plus(topic)}&hl=en-US&gl=US&ceid=US:en" if topic
           else f"{base}?hl=en-US&gl=US&ceid=US:en")
    sources = [url] + ([] if topic else ["https://feeds.bbci.co.uk/news/world/rss.xml"])
    for src in sources:
        try:
            root = ET.fromstring(requests.get(src, headers=HTTP_HEADERS, timeout=10).content)
        except (requests.RequestException, ET.ParseError):
            log.warning("news source failed: %s", src, exc_info=True)
            continue
        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            source = (item.findtext("source") or "").strip()
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3]
            items.append({"title": title, "source": source or "BBC News",
                          "url": item.findtext("link") or "", "age": _age(item.findtext("pubDate") or "")})
            if len(items) >= count:
                break
        if items:
            return items
    return []


# ---------------------------------------------------------------- live stats for the HUD

psutil.cpu_percent(interval=None)  # first call always returns 0, prime it
_gpu_cache = {"t": 0.0, "value": (None, None)}


def _gpu():
    if time.time() - _gpu_cache["t"] > 3:
        _gpu_cache["t"] = time.time()
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                 timeout=3, creationflags=NO_WINDOW).stdout.split("\n")[0].split(",")
            used, total = float(out[1]) / 1024, float(out[2]) / 1024
            _gpu_cache["value"] = (int(out[0]), f"{used:.1f}/{total:.0f} GB")
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            _gpu_cache["value"] = (None, None)
    return _gpu_cache["value"]


def stats() -> dict:
    vm = psutil.virtual_memory()
    bat = psutil.sensors_battery()
    up = int(time.time() - psutil.boot_time())
    gpu, vram = _gpu()
    return {
        "cpu": round(psutil.cpu_percent(interval=None)),
        "ram": round(vm.percent),
        "ram_total": round(vm.total / 1024 ** 3),
        "disk": round(psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").percent),
        "battery": round(bat.percent) if bat else None,
        "plugged": bool(bat.power_plugged) if bat else None,
        "gpu": gpu,
        "vram": vram,
        "uptime": f"{up // 3600}h {up % 3600 // 60}m",
    }
