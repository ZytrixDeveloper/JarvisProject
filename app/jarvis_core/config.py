"""Settings, paths and constants shared by every part of Jarvis."""
import json
import os
import platform
import sys
import threading
import uuid
from pathlib import Path

APP_NAME = "Jarvis"
APP_VERSION = "0.2.0"

# Filled in once the website + Supabase project exist. These are public values
# (the publishable/anon key is meant to ship inside apps; data is protected by RLS).
SUPABASE_URL = "https://cgiqqbibzmmqoquvyzlh.supabase.co"
SUPABASE_KEY = "sb_publishable_jX5Dk3I4xHFm2mPeeyWOJg_Ud83QuK9"
WEBSITE_URL = "https://zytrixdeveloper.github.io/JarvisProject"
GITHUB_REPO = "ZytrixDeveloper/JarvisProject"  # updates come from this repo's GitHub Releases

DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Jarvis"
MODELS_DIR = DATA_DIR / "models"
SETTINGS_FILE = DATA_DIR / "settings.json"
SESSION_FILE = DATA_DIR / "session.bin"
MEMORY_FILE = DATA_DIR / "memory.json"
LOG_FILE = DATA_DIR / "jarvis.log"
SCREENSHOT_DIR = Path.home() / "Pictures" / "Jarvis"

for _d in (DATA_DIR, MODELS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FROZEN = getattr(sys, "frozen", False)


def resource_path(rel: str) -> Path:
    """Path to a bundled file, both when running from source and from the EXE."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / rel


DEFAULTS = {
    "address_as": "sir",
    "model": "qwen3-vl:4b-instruct",
    "ollama_host": "http://127.0.0.1:11434",
    "context_size": 8192,
    "voice": "en-GB-RyanNeural",
    "voice_rate": "+4%",
    "voice_enabled": True,
    "wake_sensitivity": 0.5,
    "whisper_model": "small",
    "language": "auto",
    "camera_index": 0,
    "show_orb": True,
    "game_mode": True,
    "orb_position": [],
    "hotkey": "ctrl+alt+j",
    "protected_apps": ["whatsapp"],
    "ask_before_risky": True,
    "show_overlay": True,
    "clap_to_wake": True,
    "show_on_wake": True,
    "home_city": "",
    "start_with_windows": False,
    "device_id": "",
}


class Settings:
    """JSON-backed settings, safe to use from several threads.

    Only values the user changed are written to disk, so improved defaults in
    new versions still reach existing installs.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._user = {}
        if SETTINGS_FILE.exists():
            try:
                self._user = {k: v for k, v in json.loads(SETTINGS_FILE.read_text("utf-8-sig")).items()
                              if k in DEFAULTS and v != DEFAULTS[k]}
            except (OSError, ValueError):
                pass
        if not self._user.get("device_id"):
            self._user["device_id"] = uuid.uuid4().hex
            self.save()

    def __getitem__(self, key):
        with self._lock:
            return self._user.get(key, DEFAULTS.get(key))

    def update(self, values: dict):
        with self._lock:
            for k, v in values.items():
                if k not in DEFAULTS or k == "device_id":
                    continue
                if v == DEFAULTS[k]:
                    self._user.pop(k, None)
                else:
                    self._user[k] = v
        self.save()

    def public(self) -> dict:
        with self._lock:
            return {k: self._user.get(k, d) for k, d in DEFAULTS.items() if k != "device_id"}

    def save(self):
        with self._lock:
            SETTINGS_FILE.write_text(json.dumps(self._user, indent=2), "utf-8")


def device_name() -> str:
    return platform.node() or "Windows PC"
