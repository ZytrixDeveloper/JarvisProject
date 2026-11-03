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

from .config import MEMORY_FILE, SCREENSHOT_DIR

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


def bring_to_front(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # tap Alt so Windows allows the focus change
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


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
        self.camera = CameraFeed(settings, getattr(ctx, "camera_frame", None))
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
                existing = self._find_app_window(q, app_name)
                if existing:  # don't stack up copies: switch to the one that's open
                    bring_to_front(existing)
                    return f"{app_name} was already open, so I brought it to the front."
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"], creationflags=NO_WINDOW)
                return f"Opened {app_name}."
        exe = shutil.which(EXE_FALLBACK.get(q, q)) or shutil.which(q + ".exe")
        if exe:
            subprocess.Popen([exe], creationflags=NO_WINDOW)
            return f"Started {exe}."
        guesses = difflib.get_close_matches(q, [a[0].lower() for a in self._apps], n=3, cutoff=0.4)
        raise ToolError(f"No app called '{name}' found." + (f" Similar: {', '.join(guesses)}." if guesses else ""))

    def _find_app_window(self, query: str, app_name: str):
        targets = PROCESS_NAMES.get(query, [query.replace(" ", "")])
        me = os.getpid()
        for hwnd, title, pid in visible_windows():
            proc = _proc_name(pid)
            if pid == me or proc in CRITICAL_PROCESSES or proc == "explorer":
                continue
            if proc in targets or any(t and t in proc for t in targets) or app_name.lower() in title.lower():
                return hwnd
        return None

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
        """Get the latest news headlines (optionally about a topic) and open the holographic News Hub window.

        Args:
            topic: e.g. "technology", "football", "Slovakia"; empty for top world news
            count: how many headlines (1-10)
        """
        items = fetch_news(topic, max(1, min(10, count)))
        if not items:
            raise ToolError("I couldn't get any news right now.")
        self.ctx.show_news(topic, items)
        return "\n".join(f"- {n['title']} ({n['source']}, {n['age']})" for n in items)

    # ------------------------------------------------------------ memory & reminders

    def memory_facts(self) -> list[str]:
        try:
            return json.loads(MEMORY_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return []

    def _save_memory(self, facts: list[str]):
        MEMORY_FILE.write_text(json.dumps(facts[-60:], indent=1, ensure_ascii=False), "utf-8")

    @tool()
    def remember(self, fact: str) -> str:
        """Save a fact about the user to long-term memory (name, likes, birthday, friends, habits...).
        Use it whenever the user tells you something personal worth keeping.

        Args:
            fact: one short sentence, e.g. "Favourite game is Roblox"
        """
        facts = [f for f in self.memory_facts() if f.lower() != fact.lower()]
        self._save_memory(facts + [fact.strip()])
        return f"Remembered: {fact}"

    @tool()
    def forget(self, about: str) -> str:
        """Delete remembered facts that mention a keyword.

        Args:
            about: keyword, e.g. "birthday"
        """
        facts = self.memory_facts()
        keep = [f for f in facts if about.lower() not in f.lower()]
        self._save_memory(keep)
        return f"Forgot {len(facts) - len(keep)} fact(s) about {about}."

    @tool()
    def set_reminder(self, time: str, message: str) -> str:
        """Remind the user at a clock time (today, or tomorrow if that time has passed).

        Args:
            time: e.g. "18:30", "6pm", "7:15 am"
            message: what to remind about
        """
        m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", time.lower())
        if not m:
            raise ToolError(f"I couldn't understand the time '{time}'. Use something like 18:30.")
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm:
            hour = hour % 12 + (12 if ampm.startswith("p") else 0)
        if hour > 23 or minute > 59:
            raise ToolError(f"'{time}' isn't a valid time.")
        now = dt.datetime.now()
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= now:
            when += dt.timedelta(days=1)
        t = threading.Timer((when - now).total_seconds(), lambda: self.ctx.announce(
            f"{self.settings['address_as'].capitalize()}, a reminder: {message}"))
        t.daemon = True
        t.start()
        return f"Reminder set for {when:%H:%M}{' tomorrow' if when.date() > now.date() else ''}: {message}"

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
        parts.append(f"{user32.GetSystemMetrics(80)} monitor(s)")  # SM_CMONITORS
        return ", ".join(parts)

    @tool()
    def get_weather(self, city: str = "") -> str:
        """Current weather and today's forecast.

        Args:
            city: only a city the user actually named; leave EMPTY to use the user's own location (never guess one)
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
    def run_command(self, command: str, as_admin: bool = False) -> str:
        """Run a PowerShell command and return its output. Only when no other tool can do the job.

        Args:
            command: the PowerShell command
            as_admin: true only if the command needs administrator rights (Windows will show its UAC prompt)
        """
        if DANGEROUS_COMMANDS.search(command):
            raise ToolError("That command could damage the system, so it's blocked.")
        if not self.ctx.confirm(f"Run this command{' as administrator' if as_admin else ''}?\n{command}"):
            return "The user cancelled."
        if as_admin:
            return self._run_elevated(command)
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                             capture_output=True, encoding="utf-8", errors="replace", timeout=90,
                             creationflags=NO_WINDOW)
        text = (out.stdout + ("\n" + out.stderr if out.stderr else "")).strip()
        return f"exit code {out.returncode}\n{text[:2500] or '(no output)'}"

    def _run_elevated(self, command: str) -> str:
        """Run via an elevated PowerShell. Windows asks the user (UAC) every time."""
        import base64
        import tempfile
        out_file = os.path.join(tempfile.gettempdir(), f"jarvis_admin_{os.getpid()}.txt")
        script = f"$ErrorActionPreference='Continue'; & {{ {command} }} *> '{out_file}'"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        launcher = ("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                    f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'")
        res = subprocess.run(["powershell", "-NoProfile", "-Command", launcher], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=300, creationflags=NO_WINDOW)
        if res.returncode != 0:
            return "The administrator prompt was declined, so nothing was run."
        try:
            text = Path(out_file).read_text("utf-16", errors="replace").strip()
        except (OSError, UnicodeError):
            text = ""
        finally:
            Path(out_file).unlink(missing_ok=True)
        return f"Ran as administrator.\n{text[:2500] or '(no output)'}"

    @tool()
    def build_project(self, request: str) -> str:
        """Write a complete small software project as real files (website, web game, Python script, tool)
        in Documents/Jarvis Projects and open it.

        Args:
            request: what to build, including every detail the user gave (purpose, style, features)
        """
        name, files = self.ctx.build_code(request)
        folder = known_folder("documents") / "Jarvis Projects" / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        for rel, content in files:
            target = (folder / rel).resolve()
            if folder.resolve() not in target.parents:
                continue  # never write outside the project folder
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")
        entry = folder / "index.html"
        os.startfile(entry if entry.exists() else folder)
        return (f"Built '{name}' with {len(files)} files ({', '.join(f for f, _ in files)}) in {folder} "
                f"and opened {'it in the browser' if entry.exists() else 'the folder'}.")

    @tool()
    def design_3d(self, project: str) -> str:
        """Design a 3D model plus a step-by-step build plan for a physical object or project
        (car, house, robot, drone, desk, rocket...) and open it in the 3D Lab, where the user can move the parts.

        Args:
            project: what to design, including any details the user gave
        """
        design = self.ctx.design(project)
        folder = known_folder("documents") / "Jarvis Designs"
        folder.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w\- ]", "", design["title"]).strip().replace(" ", "_")[:40] or "design"
        path = folder / f"{slug}_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(design, indent=1, ensure_ascii=False), "utf-8")
        self.ctx.open_lab(design)
        return (f"Opened '{design['title']}' in the 3D Lab: {len(design['parts'])} parts, "
                f"{len(design['steps'])} build steps. Saved to {path}.")

    # ------------------------------------------------------------ vision

    def _active_monitor(self, monitors) -> int:
        """Index (1..n) of the monitor showing the window the user last used."""
        rect = wintypes.RECT()
        if self.focus.last and user32.GetWindowRect(self.focus.last, ctypes.byref(rect)):
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            for i, m in enumerate(monitors[1:], 1):
                if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                    return i
        return 1

    def _grab_screen(self, screen: int = -1):
        import mss
        from PIL import Image
        hidden = self.ctx.hide_hud()
        try:
            with mss.mss() as sct:
                mons = sct.monitors  # [0] = all monitors together, [1..n] = each monitor
                index = self._active_monitor(mons) if screen < 0 else screen if screen < len(mons) else 0
                shot = sct.grab(mons[index])
        finally:
            if hidden:
                self.ctx.show_hud()
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    @tool()
    def look_at_screen(self, question: str = "Describe what is on the screen.", screen: int = -1) -> str:
        """Look at the user's screen and answer a question about it (read errors, describe, help).

        Args:
            question: what to find out from the screen
            screen: -1 = the monitor the user is working on (default), 0 = all monitors, 1/2/3 = that monitor
        """
        jpeg = _jpeg(self._grab_screen(screen), max_side=1600 if screen == 0 else 1280)
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
        frame = self.camera.latest_frame()  # reuse the live view if it's running
        if frame is None:
            cam = cv2.VideoCapture(int(self.settings["camera_index"]), cv2.CAP_DSHOW)
            try:
                if not cam.isOpened():
                    raise ToolError("That camera isn't available. Pick another one in Settings.")
                cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                for _ in range(12):  # let auto-exposure settle
                    ok, f = cam.read()
                    if ok:
                        frame = f
                if frame is None:
                    raise ToolError("The camera didn't return an image.")
            finally:
                cam.release()
        jpeg = _jpeg(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
        self.ctx.capture("camera", jpeg)
        return self.ctx.vision(question, jpeg)

    @tool()
    def take_screenshot(self, screen: int = 0) -> str:
        """Save a screenshot to Pictures/Jarvis.

        Args:
            screen: 0 = all monitors (default), 1/2/3 = that monitor, -1 = the one the user is working on
        """
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"screenshot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        image = self._grab_screen(screen)
        image.save(path)
        self.ctx.capture("screen", _jpeg(image, 640))
        return f"Screenshot saved to {path}."


# ---------------------------------------------------------------- cameras

def list_cameras() -> list[str]:
    """Camera names in the same order OpenCV's DirectShow indexes them."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        return FilterGraph().get_input_devices()
    except Exception:  # noqa: BLE001
        log.warning("camera list failed", exc_info=True)
        return []


class CameraFeed:
    """Live camera view for the HUD. While it runs, scans reuse its newest frame."""

    def __init__(self, settings, on_frame):
        self.settings = settings
        self.on_frame = on_frame
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def latest_frame(self):
        with self._lock:
            return None if self._latest is None or not self.running else self._latest.copy()

    def _run(self):
        import base64
        import cv2
        cam = cv2.VideoCapture(int(self.settings["camera_index"]), cv2.CAP_DSHOW)
        try:
            if not cam.isOpened():
                if self.on_frame:
                    self.on_frame(None)
                return
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            last = 0.0
            while not self._stop.is_set():
                ok, frame = cam.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                with self._lock:
                    self._latest = frame
                if self.on_frame and time.time() - last > 0.12:  # ~8 fps preview
                    last = time.time()
                    small = cv2.resize(frame, (480, int(480 * frame.shape[0] / frame.shape[1])))
                    ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok:
                        self.on_frame(base64.b64encode(buf.tobytes()).decode())
        finally:
            cam.release()
            with self._lock:
                self._latest = None


# ---------------------------------------------------------------- news

_live_cache = {}


def live_video_id(channel_id: str):
    """Video ID of a YouTube channel's current live stream (cached 20 min), or None."""
    hit = _live_cache.get(channel_id)
    if hit and time.time() - hit[0] < 1200:
        return hit[1]
    try:
        # SOCS=CAI is YouTube's "rejected non-essential cookies" answer; without it EU users get a consent page.
        html = requests.get(f"https://www.youtube.com/channel/{channel_id}/live", timeout=12,
                            headers={**HTTP_HEADERS, "Cookie": "SOCS=CAI", "Accept-Language": "en-US,en;q=0.9"}).text
    except requests.RequestException:
        return None
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
    vid = m.group(1) if m and '"isLive":true' in html else None
    _live_cache[channel_id] = (time.time(), vid)
    return vid


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
    days = minutes // 1440
    return f"{days} day{'s' if days > 1 else ''} ago"


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
            src_el = item.find("source")
            source = (src_el.text or "").strip() if src_el is not None else ""
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3]
            src_url = src_el.get("url", "") if src_el is not None else "https://www.bbc.co.uk"
            items.append({"title": title, "source": source or "BBC News",
                          "domain": urllib.parse.urlparse(src_url).netloc,
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
