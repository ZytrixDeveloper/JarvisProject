"""Wires everything together: HUD window, brain, voice, tools, account and tray."""
import base64
import ctypes
import datetime as dt
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
import winreg

import webview

from . import tools as tools_mod
from .account import Account
from .brain import Brain
from .config import APP_NAME, APP_VERSION, DATA_DIR, FROZEN, Settings, resource_path
from .overlay import Overlay
from .tools import Toolbox
from .voice import VOICES, Voice

log = logging.getLogger("jarvis.app")
user32 = ctypes.windll.user32
WINDOW_TITLE = "J.A.R.V.I.S."

# What the glowing screen overlay says while a tool runs.
TOOL_CAPTIONS = {
    "open_app": "Opening {name}", "close_app": "Closing {name}", "list_open_windows": "Scanning open windows",
    "open_website": "Opening {url}", "google_search": "Searching Google: {query}",
    "search_web": "Searching the web: {query}", "play_youtube": "Playing {query} on YouTube",
    "get_news": "Opening the News Hub", "read_clipboard": "Reading your clipboard",
    "system_status": "Running diagnostics", "get_weather": "Checking the weather", "volume": "Adjusting volume",
    "media_control": "Controlling media playback", "set_brightness": "Adjusting brightness",
    "power": "Power control: {action}", "set_timer": "Setting a timer", "set_reminder": "Setting a reminder",
    "remember": "Saving to memory", "forget": "Updating memory", "type_text": "Typing for you",
    "press_keys": "Pressing {keys}", "find_files": "Searching your files: {name}", "open_path": "Opening {path}",
    "read_file": "Reading {path}", "write_file": "Writing {path}", "create_folder": "Creating folder {path}",
    "move_file": "Moving {source}", "delete_file": "Moving {path} to the Recycle Bin",
    "run_command": "Running a command", "look_at_screen": "Analysing your screen",
    "look_through_camera": "Scanning through the camera", "take_screenshot": "Taking a screenshot",
}


def tool_caption(name, args) -> str:
    template = TOOL_CAPTIONS.get(name, "Working")
    try:
        text = template.format(**{k: str(v)[:40] for k, v in (args or {}).items()})
    except (KeyError, IndexError):
        text = template.split(" {")[0].split(":")[0]
    return f"JARVIS  ·  {text}"


class NewsApi:
    """Functions the News Hub window's JavaScript can call."""

    def __init__(self, app):
        self._app = app

    def initial_topic(self):
        return self._app.news_topic

    def load(self, topic=""):
        return tools_mod.fetch_news(topic or "", 18)

    def open_url(self, url):
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            import webbrowser
            webbrowser.open(url)

    def close(self):
        self._app.close_news_hub()


class Api:
    """Functions the HUD's JavaScript can call (window.pywebview.api.*)."""

    def __init__(self, app):
        self._app = app

    def ready(self):
        a = self._app
        a.ui_ready.set()
        return {"version": APP_VERSION, "user": a.account.user, "accountConfigured": a.account.configured,
                "settings": a.settings.public(), "voices": VOICES, "hotkey": a.settings["hotkey"]}

    def send_text(self, text):
        text = (text or "").strip()
        if text:
            threading.Thread(target=self._app.handle_command, args=(text, "text"), daemon=True).start()

    def mic(self):
        self._app.voice.trigger()

    def set_mic(self, on):
        self._app.voice.set_mic(bool(on))

    def stop_speaking(self):
        self._app.voice.stop_speaking()

    def confirm_answer(self, cid, yes):
        self._app.answer_confirm(cid, bool(yes))

    def new_conversation(self):
        self._app.brain.reset()

    def link_account(self):
        self._app.account.start_link(self._app.on_linked)

    def sign_out(self):
        self._app.account.sign_out()
        self._app.emit("account", {"user": None, "required": self._app.account.configured})

    def save_settings(self, values):
        self._app.save_settings(values or {})
        return self._app.settings.public()

    def open_data_folder(self):
        os.startfile(DATA_DIR)

    def open_url(self, url):
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            import webbrowser
            webbrowser.open(url)

    def refresh_news(self, topic=""):
        threading.Thread(target=self._app.load_news, args=(topic or "",), daemon=True).start()

    def open_news_hub(self):
        self._app.open_news_hub("")

    def minimize(self):
        self._app.window.minimize()

    def toggle_maximize(self):
        w = self._app.window
        w.restore() if getattr(w, "_jarvis_max", False) else w.maximize()
        w._jarvis_max = not getattr(w, "_jarvis_max", False)

    def close(self):
        self._app.hide_to_tray()

    def quit(self):
        self._app.quit()


class JarvisApp:
    def __init__(self, quiet=False, require_account=True):
        self.quiet = quiet  # --no-voice: no microphone, no speaking (for testing)
        self.settings = Settings()
        self.window = None
        self.news_window = None
        self.news_topic = ""
        self.tray = None
        self.overlay = Overlay()
        self._events = queue.Queue()
        self.ui_ready = threading.Event()
        self._busy = threading.Lock()
        self._confirm = None
        self.toolbox = Toolbox(self.settings, self)
        self.brain = Brain(self.settings, self.toolbox, self.emit)
        self.voice = Voice(self.settings, self.handle_command, self.emit)
        if quiet:
            self.voice.speak = lambda text: None
        self.account = Account(self.settings)
        self.account.enabled = require_account

    # ------------------------------------------------------------ events to the HUD

    def emit(self, kind, data=None):
        if kind == "tool" and data and data.get("state") == "run" and self.settings["show_overlay"]:
            self.overlay.show(tool_caption(data["name"], data.get("args")))
        self._events.put({"type": kind, **(data or {})})

    def _pump(self):
        self.ui_ready.wait(30)  # the HUD calls api.ready() once its JavaScript is listening
        while True:
            evt = self._events.get()
            if self.window is None:
                continue
            try:
                self.window.evaluate_js(f"window.hud && window.hud.on({json.dumps(evt)})")
            except Exception:  # noqa: BLE001 - window may be closing
                log.debug("evaluate_js failed", exc_info=True)

    def _stats_loop(self):
        while True:
            self.emit("stats", tools_mod.stats())
            time.sleep(2)

    def load_news(self, topic=""):
        items = tools_mod.fetch_news(topic, 8)
        if items:
            self.show_news(topic, items, open_hub=False)

    def _news_loop(self):
        while True:
            try:
                self.load_news()
            except Exception:  # noqa: BLE001 - offline etc., try again later
                log.warning("news refresh failed", exc_info=True)
            try:
                self.emit("weather", {"text": self.toolbox.get_weather("")})
            except Exception:  # noqa: BLE001
                log.warning("weather refresh failed", exc_info=True)
            time.sleep(15 * 60)

    # ------------------------------------------------------------ the main command path

    def handle_command(self, text, source="text"):
        if self.account.configured and not self.account.user:
            self.emit("notice", {"text": "Link your account first."})
            self.voice.busy(False)
            return
        if not self._busy.acquire(blocking=False):
            self.emit("notice", {"text": "Still working on the last request."})
            return
        try:
            self.voice.busy(True)
            self.emit("heard", {"text": text, "source": source})
            self.emit("state", {"state": "thinking"})
            reply = self.brain.ask(text)
            self.emit("reply", {"text": reply})
            self.emit("state", {"state": "speaking"})
            self.voice.speak(reply)
        except Exception:  # noqa: BLE001 - never let one bad request kill the app
            log.exception("command failed")
            self.emit("reply", {"text": "Something went wrong on my side. The details are in the log."})
        finally:
            self.overlay.hide()
            self.emit("state", {"state": "idle"})
            self.voice.busy(False)
            self._busy.release()

    # ------------------------------------------------------------ ctx used by tools

    def confirm(self, question: str) -> bool:
        if not self.settings["ask_before_risky"]:
            return True
        cid = secrets.token_hex(4)
        done, result = threading.Event(), {"yes": False}
        self._confirm = (cid, done, result)
        self.emit("confirm", {"id": cid, "text": question, "timeout": 25})
        self.emit("state", {"state": "confirm"})
        if self.settings["show_overlay"]:
            self.overlay.show("JARVIS  ·  Waiting for your confirmation")
        self.voice.speak(f"{question.splitlines()[0]} Shall I proceed, {self.settings['address_as']}?")

        def by_voice():
            answer = self.voice.ask_yes_no(timeout=20)
            if answer is not None and not done.is_set():
                result["yes"] = answer
                done.set()

        threading.Thread(target=by_voice, daemon=True).start()
        done.wait(25)
        self._confirm = None
        self.emit("confirm_done", {"id": cid, "yes": result["yes"]})
        self.emit("state", {"state": "thinking"})
        return result["yes"]

    def answer_confirm(self, cid, yes):
        if self._confirm and self._confirm[0] == cid:
            self._confirm[2]["yes"] = yes
            self._confirm[1].set()

    def announce(self, text):
        self.emit("reply", {"text": text, "announce": True})
        threading.Thread(target=self.voice.speak, args=(text,), daemon=True).start()

    def show_news(self, topic, items, open_hub=True):
        self.emit("news", {"topic": topic, "items": items})
        if open_hub:
            self.open_news_hub(topic)

    def open_news_hub(self, topic=""):
        self.news_topic = topic
        if self.news_window is not None:
            try:
                self.news_window.show()
                self.news_window.restore()
                self.news_window.evaluate_js(f"window.newsHub && newsHub.show({json.dumps(topic)})")
                return
            except Exception:  # noqa: BLE001 - window was closed behind our back
                self.news_window = None
        self.news_window = webview.create_window(
            "J.A.R.V.I.S. News Hub", str(resource_path("ui/news.html")), js_api=NewsApi(self),
            width=1040, height=700, min_size=(620, 480), frameless=True, easy_drag=False,
            background_color="#02070c")
        self.news_window.events.closed += lambda: setattr(self, "news_window", None)

    def close_news_hub(self):
        if self.news_window is not None:
            window, self.news_window = self.news_window, None
            window.destroy()

    def capture(self, kind, jpeg):
        self.emit("capture", {"kind": kind, "img": base64.b64encode(jpeg).decode()})

    def vision(self, question, jpeg):
        self.emit("state", {"state": "scanning"})
        try:
            return self.brain.vision(question, jpeg)
        finally:
            self.emit("state", {"state": "thinking"})

    def _hud_hwnd(self):
        me = os.getpid()
        for hwnd, title, pid in tools_mod.visible_windows():
            if pid == me and title == WINDOW_TITLE:
                return hwnd
        return None

    def hide_hud(self) -> bool:
        hwnd = self._hud_hwnd()
        if hwnd and not user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            self._hidden_hwnd = hwnd
            time.sleep(0.35)
            return True
        return False

    def show_hud(self):
        hwnd = getattr(self, "_hidden_hwnd", None)
        if hwnd:
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            self._hidden_hwnd = None

    # ------------------------------------------------------------ account

    def on_linked(self, user):
        self.emit("account", {"user": user, "required": True})
        self.announce(f"Account linked. Welcome, {user['name']}.")

    # ------------------------------------------------------------ settings

    def save_settings(self, values):
        allowed = {"address_as", "model", "voice", "voice_rate", "voice_enabled", "wake_sensitivity",
                   "protected_apps", "ask_before_risky", "home_city", "start_with_windows", "whisper_model",
                   "show_overlay"}
        clean = {k: v for k, v in values.items() if k in allowed}
        if isinstance(clean.get("protected_apps"), str):
            clean["protected_apps"] = [p.strip() for p in clean["protected_apps"].split(",") if p.strip()]
        if "wake_sensitivity" in clean:
            clean["wake_sensitivity"] = max(0.1, min(0.8, float(clean["wake_sensitivity"])))
        self.settings.update(clean)
        if "start_with_windows" in clean:
            set_autostart(bool(clean["start_with_windows"]))

    # ------------------------------------------------------------ startup

    def _step(self, key, label, status, detail=""):
        self.emit("boot", {"key": key, "label": label, "status": status, "detail": detail})

    def boot(self):
        threading.Thread(target=self._pump, daemon=True).start()
        time.sleep(0.4)
        self._step("core", "Core systems", "ok", f"v{APP_VERSION}")

        if self.account.configured:
            self._step("account", "Account link", "run")
            user, msg = self.account.restore()
            if user:
                self._step("account", "Account link", "ok", user["email"] + (" (offline)" if msg == "offline" else ""))
                self.account.keep_alive()
            else:
                self._step("account", "Account link", "warn", f"Sign in required: {msg}")
            self.emit("account", {"user": user, "required": True})
        else:
            self._step("account", "Account link", "warn", "Website login not configured yet")

        self._step("ollama", "Neural core (Ollama)", "run")
        if self.brain.ensure_ollama():
            self._step("ollama", "Neural core (Ollama)", "ok")
            self._boot_model()
        else:
            self._step("ollama", "Neural core (Ollama)", "fail", "Install Ollama from ollama.com, then restart Jarvis")

        threading.Thread(target=self._boot_voice, daemon=True).start()
        threading.Thread(target=self._stats_loop, daemon=True).start()
        threading.Thread(target=self._news_loop, daemon=True).start()
        self._register_hotkey()

    def _boot_model(self):
        model = self.settings["model"]
        try:
            if not self.brain.has_model():
                self._step("model", f"AI model {model}", "run", "Downloading (first start only)...")

                def progress(status, done, total):
                    pct = done * 100 // total
                    self._step("model", f"AI model {model}", "run",
                               f"Downloading {pct}% ({done / 1e9:.1f} / {total / 1e9:.1f} GB)")

                self.brain.pull_model(progress)
            self._step("model", f"AI model {model}", "run", "Loading into GPU...")
            self.brain.warm_up()
            self._step("model", f"AI model {model}", "ok", "Loaded")
        except Exception as e:  # noqa: BLE001
            log.exception("model setup failed")
            self._step("model", f"AI model {model}", "fail", str(e)[:120])

    def _boot_voice(self):
        if self.quiet:
            self._step("mic", "Microphone", "warn", "Disabled (--no-voice test mode)")
            self._finish_boot()
            return
        try:
            self.voice.start_mic()
            self._step("mic", "Microphone", "ok")
        except Exception as e:  # noqa: BLE001
            log.exception("mic failed")
            self._step("mic", "Microphone", "fail", f"No microphone: {e}"[:120])
            self._finish_boot()
            return
        self._step("stt", "Speech recognition", "run", "Loading (first start downloads ~0.5 GB)...")
        try:
            self.voice.load_speech_to_text()
            self._step("stt", "Speech recognition", "ok", self.settings["whisper_model"])
        except Exception as e:  # noqa: BLE001
            log.exception("stt failed")
            self._step("stt", "Speech recognition", "fail", str(e)[:120])
        self._step("wake", 'Wake word "Hey Jarvis"', "run")
        try:
            self.voice.load_wake_word()
            self._step("wake", 'Wake word "Hey Jarvis"', "ok")
        except Exception as e:  # noqa: BLE001
            log.exception("wake word failed")
            self._step("wake", 'Wake word "Hey Jarvis"', "fail", str(e)[:120])
        self._finish_boot()

    def _finish_boot(self):
        self.emit("ready", {})
        hour = dt.datetime.now().hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        name = self.account.user["name"] if self.account.user else self.settings["address_as"]
        if self.account.configured and not self.account.user:
            self.announce(f"Good {part}. Please link your account to activate me.")
        else:
            self.announce(f"Good {part}, {name}. All systems are online.")
        if getattr(self, "autorun", None):
            threading.Thread(target=self.handle_command, args=(self.autorun, "text"), daemon=True).start()

    def _register_hotkey(self):
        try:
            import keyboard
            keyboard.add_hotkey(self.settings["hotkey"], self.voice.trigger)
        except Exception:  # noqa: BLE001
            log.exception("hotkey registration failed")

    # ------------------------------------------------------------ tray & window

    def _start_tray(self):
        try:
            import pystray
            from .icon import reactor_icon
            menu = pystray.Menu(
                pystray.MenuItem("Show Jarvis", lambda: self.show_window(), default=True),
                pystray.MenuItem("Listen", lambda: self.voice.trigger()),
                pystray.MenuItem("Microphone on", lambda: self.voice.set_mic(not self.voice.mic_on),
                                 checked=lambda item: self.voice.mic_on),
                pystray.MenuItem("Quit", lambda: self.quit()),
            )
            self.tray = pystray.Icon("jarvis", reactor_icon(64), "J.A.R.V.I.S.", menu)
            self.tray.run_detached()
        except Exception:  # noqa: BLE001
            log.exception("tray failed")
            self.tray = None

    def show_window(self):
        if self.window:
            self.window.show()
            self.window.restore()

    def hide_to_tray(self):
        if self.tray:
            self.window.hide()
            self.tray.notify("Still running. Say \"Hey Jarvis\" or use the tray icon.", "J.A.R.V.I.S.")
        else:
            self.quit()

    def quit(self):
        try:
            if self.tray:
                self.tray.stop()
        finally:
            if self.window:
                self.window.destroy()
            os._exit(0)

    def run(self):
        self.window = webview.create_window(
            WINDOW_TITLE, str(resource_path("ui/index.html")), js_api=Api(self),
            width=1240, height=780, min_size=(980, 640), frameless=True, easy_drag=False,
            background_color="#03070d")
        self._start_tray()
        webview.start(self.boot, private_mode=False, storage_path=str(DATA_DIR / "webview"))


def set_autostart(enabled: bool):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if FROZEN:
                cmd = f'"{sys.executable}"'
            else:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                cmd = f'"{pyw}" "{os.path.abspath(sys.argv[0])}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
