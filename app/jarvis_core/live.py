"""What's happening on the PC right now: the song that's playing and whether you're in a call.

Now Playing uses Windows' own media controls (the ones behind the volume flyout), so it works
with Spotify, YouTube in any browser, Apple Music, VLC and others.
Calls are detected from which apps are using the microphone right now (Windows tracks this for
the privacy icon) plus their window titles. Jarvis never listens to or records the call.
"""
import asyncio
import base64
import logging
import os
import sys
import re
import threading
import time
import winreg

log = logging.getLogger("jarvis.live")

APP_NAMES = {
    "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "opera": "Opera",
    "brave": "Brave", "vlc": "VLC", "applemusic": "Apple Music", "zunemusic": "Media Player",
    "itunes": "iTunes", "deezer": "Deezer", "tidal": "TIDAL", "youtube": "YouTube Music",
    "whatsapp": "WhatsApp", "discord": "Discord", "amazonmusic": "Amazon Music", "soundcloud": "SoundCloud",
}

# Apps that make calls -> display name. Browsers count only with a call site open (Meet, Teams...).
CALL_APPS = {
    "whatsapp": "WhatsApp", "discord": "Discord", "teams": "Microsoft Teams", "zoom": "Zoom", "skype": "Skype",
    "telegram": "Telegram", "slack": "Slack", "signal": "Signal", "messenger": "Messenger", "webex": "Webex",
    "roblox": "Roblox voice chat", "steam": "Steam voice chat",
}
BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave")
WEB_CALLS = re.compile(r"\b(meet|zoom|teams|discord|messenger|whatsapp|call|hovor|anruf|rozmowa)\b", re.I)
CALL_WORDS = re.compile(r"(call|hovor|anruf|appel|llamada|chiamata|rozmowa|meeting|schôdz|voice|video)", re.I)


def app_name(app_id: str) -> str:
    low = (app_id or "").lower()
    for key, name in APP_NAMES.items():
        if key in low:
            return name
    return re.sub(r"\.exe$", "", app_id.split("!")[0].split("_")[0], flags=re.I) or "Media"


# ---------------------------------------------------------------- now playing

class MediaWatcher:
    """Polls Windows' media session every 1.5 s and calls on_change(info or None) when it changes."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.current = None
        self._loop = None
        self._mgr = None

    def start(self):
        threading.Thread(target=self._run, name="media", daemon=True).start()

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            self._loop.run_until_complete(self._watch())
        except Exception:  # noqa: BLE001 - Now Playing is optional
            log.exception("media watcher stopped")

    async def _watch(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
        self._mgr = await Manager.request_async()
        last_key, last_emit = None, 0.0
        while True:
            try:
                info = await self._read(last_key)
            except Exception:  # noqa: BLE001 - a player closing mid-read etc.
                log.debug("media read failed", exc_info=True)
                info = None
            key = (info["app"], info["title"], info["artist"], info["playing"]) if info else None
            if key != last_key or (info and time.time() - last_emit > 10):  # resync the progress bar
                if info and last_key and key[:3] == last_key[:3] and self.current:
                    info["art"] = self.current.get("art")  # same song: don't resend the cover
                self.current = info
                self.on_change(info)
                last_key, last_emit = key, time.time()
            await asyncio.sleep(1.5)

    async def _read(self, last_key):
        session = self._mgr.get_current_session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        if not props or not (props.title or "").strip():
            return None
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()
        app_id = session.source_app_user_model_id or ""
        info = {
            "app": app_name(app_id), "title": props.title, "artist": props.artist or props.album_artist or "",
            "album": props.album_title or "", "playing": int(playback.playback_status) == 4,
            "position": timeline.position.total_seconds() if timeline.position else 0,
            "duration": timeline.end_time.total_seconds() if timeline.end_time else 0,
            "updated": time.time(), "art": None,
        }
        if timeline.last_updated_time and info["playing"]:  # Windows only refreshes position now and then
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
            if 0 < age < 3600:
                info["position"] = min(info["duration"] or 1e9, info["position"] + age)
        same_song = last_key and last_key[:3] == (info["app"], info["title"], info["artist"])
        if props.thumbnail and not same_song:
            info["art"] = await _read_thumbnail(props.thumbnail)
        return info

    def control(self, action: str) -> bool:
        """play_pause / next / previous / stop through the player itself. False if it couldn't."""
        if not self._loop or not self._mgr:
            return False
        future = asyncio.run_coroutine_threadsafe(self._control(action), self._loop)
        try:
            return bool(future.result(timeout=5))
        except Exception:  # noqa: BLE001
            return False

    async def _control(self, action):
        session = self._mgr.get_current_session()
        if session is None:
            return False
        calls = {"play_pause": session.try_toggle_play_pause_async, "next": session.try_skip_next_async,
                 "previous": session.try_skip_previous_async, "stop": session.try_stop_async}
        return await calls[action]()


async def _read_thumbnail(ref):
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    stream = await ref.open_read_async()
    size = min(int(stream.size), 4_000_000)
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    data = bytes(memoryview(buf))
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ---------------------------------------------------------------- calls

_MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def mic_users() -> list[str]:
    """Apps using the microphone right now (lower-case names), excluding Jarvis itself."""
    me = os.path.basename(sys.executable).lower()
    users = []

    def scan(path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if sub == "NonPackaged":
                    scan(path + "\\NonPackaged")
                    continue
                try:
                    with winreg.OpenKey(key, sub) as k:
                        start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop:
                    name = sub.split("#")[-1].lower()
                    if name not in (me, "jarvis.exe", "python.exe", "pythonw.exe"):
                        users.append(name)

    scan(_MIC_KEY)
    return users


def call_status(windows) -> dict:
    """{"in_call": bool, "app": "WhatsApp", "detail": "Voice call"}. windows = [(hwnd, title, process)]."""
    for user in mic_users():
        browser = next((b for b in BROWSERS if b in user), None)
        if browser:
            titles = [t for _, t, proc in windows if browser in proc and WEB_CALLS.search(t)]
            if titles:
                return {"in_call": True, "app": app_name(browser), "detail": _short(titles[0])}
            continue
        app = next((name for key, name in CALL_APPS.items() if key in user), None)
        if app:
            key = next(k for k, v in CALL_APPS.items() if v == app)
            titles = [t for _, t, proc in windows if key in proc and CALL_WORDS.search(t)]
            return {"in_call": True, "app": app, "detail": _short(titles[0]) if titles else "Call in progress"}
    return {"in_call": False}


def _short(title: str) -> str:
    title = re.sub(r"\s*[-|–]\s*(Google Chrome|Microsoft Edge|Mozilla Firefox|Opera|Brave)$", "", title)
    return title[:60]
