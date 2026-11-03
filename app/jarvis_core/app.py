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
from . import live, updater
from .live import MediaWatcher
from .account import Account
from .brain import Brain, SentenceSplitter
from .config import APP_NAME, APP_VERSION, DATA_DIR, FROZEN, Settings, resource_path
from ctypes import wintypes

from .overlay import Overlay, active_work_area, fullscreen_app_active
from .tools import Toolbox
from .voice import LANGUAGES, VOICES, Voice

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
    "design_3d": "Designing {project} in the 3D Lab", "build_project": "Building {request}",
    "now_playing": "Checking what's playing", "call_status": "Checking your calls",
}


def tool_caption(name, args) -> str:
    template = TOOL_CAPTIONS.get(name, "Working")
    try:
        text = template.format(**{k: str(v)[:40] for k, v in (args or {}).items()})
    except (KeyError, IndexError):
        text = template.split(" {")[0].split(":")[0]
    return f"JARVIS  ·  {text}"


class LabApi:
    """Functions the 3D Lab window's JavaScript can call."""

    def __init__(self, app):
        self._app = app

    def get_design(self):
        return self._app.lab_design

    def redesign(self, prompt):
        design = self._app.brain.design(prompt)
        self._app.lab_design = design
        return design

    def save(self, design):
        folder = tools_mod.known_folder("documents") / "Jarvis Designs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"design_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(design, indent=1, ensure_ascii=False), "utf-8")
        return str(path)

    def close(self):
        self._app.close_lab()


class NewsApi:
    """Functions the News Hub window's JavaScript can call."""

    def __init__(self, app):
        self._app = app

    def initial_topic(self):
        return self._app.news_topic

    def load(self, topic=""):
        return tools_mod.fetch_news(topic or "", 18)

    def live_video(self, channel_id):
        return tools_mod.live_video_id(str(channel_id))

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
                "settings": a.settings.public(), "voices": VOICES, "languages": LANGUAGES,
                "hotkey": a.settings["hotkey"], "installed": FROZEN}

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

    def check_updates(self):
        threading.Thread(target=self._app.check_updates, args=(True,), daemon=True).start()

    def list_cameras(self):
        return tools_mod.list_cameras()

    def media(self, action):
        if action in ("play_pause", "next", "previous"):
            threading.Thread(target=self._app.media_control, args=(action,), daemon=True).start()

    def camera_live(self, on):
        cam = self._app.toolbox.camera
        cam.start() if on else cam.stop()

    def open_lab(self):
        self._app.open_lab(self._app.lab_design)

    def uninstall(self, remove_models):
        return self._app.uninstall(bool(remove_models))

    def install_update(self):
        threading.Thread(target=self._app.install_update, daemon=True).start()

    def minimize(self):
        self._app.window.minimize()

    def toggle_maximize(self):
        self._app.set_fullscreen(not self._app.fullscreen)

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
        self.lab_window = None
        self.lab_design = None
        self._hud = None
        self.fullscreen = False
        self._events = queue.Queue()
        self.ui_ready = threading.Event()
        self._busy = threading.Lock()
        self._confirm = None
        self.toolbox = Toolbox(self.settings, self)
        self.brain = Brain(self.settings, self.toolbox, self.emit)
        self.background = False  # --background: start hidden in the tray (used by "start with Windows")
        self.voice = Voice(self.settings, self.handle_command, self.emit)
        self.voice.on_wake = self._on_wake
        if quiet:
            self.voice.speak = lambda text: None
        self.overlay = Overlay(self.settings, on_open=self.show_window, on_listen=self.voice.trigger,
                               hud_visible=self.hud_shown)
        self.account = Account(self.settings)
        self.account.enabled = require_account
        self.media = MediaWatcher(lambda info: self.emit("now_playing", info or {"none": True}))
        self.call = {"in_call": False}

    def silent_now(self) -> bool:
        """In a call (and the setting is on): reply on screen only, never talk into the call."""
        return bool(self.settings["quiet_in_calls"] and self.call.get("in_call"))

    def _calls_loop(self):
        while True:
            try:
                me = os.getpid()
                windows = [(h, t, tools_mod._proc_name(pid)) for h, t, pid in tools_mod.visible_windows() if pid != me]
                status = live.call_status(windows)
            except Exception:  # noqa: BLE001
                log.debug("call check failed", exc_info=True)
                status = {"in_call": False}
            if status != self.call:
                self.call = status
                self.emit("call", status)
            time.sleep(3)

    def hud_shown(self) -> bool:
        """Cheap check (called 30x a second by the orb): is the HUD on screen and not minimised?"""
        if not self._hud or not user32.IsWindow(self._hud):
            self._hud = user32.FindWindowW(None, WINDOW_TITLE)
        return bool(self._hud) and bool(user32.IsWindowVisible(self._hud)) and not user32.IsIconic(self._hud)

    # ------------------------------------------------------------ events to the HUD

    def emit(self, kind, data=None):
        if kind == "tool" and data and data.get("state") == "run" and self.settings["show_overlay"]:
            self.overlay.show(tool_caption(data["name"], data.get("args")))
        elif kind == "state" and data:
            self.overlay.set_state(data["state"])
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
            # Speak while the reply is still streaming in: sentence 1 plays while sentence 2 is made.
            silent = self.quiet or self.silent_now()
            if self.silent_now():
                self.emit("notice", {"text": f"You're in a call on {self.call['app']}, so I'll reply silently."})
            stream = None if silent else self.voice.stream()
            player = threading.Thread(target=stream.play, daemon=True) if stream else None
            if player:
                player.start()
            splitter = SentenceSplitter(stream.add) if stream else None
            streamed = []

            def on_delta(delta):
                streamed.append(delta)
                self.emit("reply_delta", {"text": delta})
                if splitter:
                    splitter.feed(delta)

            reply = self.brain.ask(text, on_delta=on_delta)
            self.emit("reply", {"text": reply})
            if stream:
                if streamed:
                    splitter.flush()
                else:  # error messages etc. arrive without streaming
                    stream.add(reply)
                stream.finish()
                self.emit("state", {"state": "speaking"})
                player.join(timeout=180)
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
        if self.silent_now():
            self.emit("notice", {"text": text})
            return
        threading.Thread(target=self.voice.speak, args=(text,), daemon=True).start()

    def media_control(self, action) -> bool:
        return self.media.control(action)

    def now_playing(self):
        return self.media.current

    def call_info(self):
        return self.call

    def camera_frame(self, img):
        self.emit("camera_frame", {"img": img} if img else {"error": "That camera isn't available."})

    def build_code(self, request):
        return self.brain.build_project(request)

    def design(self, project):
        self.emit("state", {"state": "scanning"})
        try:
            return self.brain.design(project)
        finally:
            self.emit("state", {"state": "thinking"})

    def open_lab(self, design):
        self.lab_design = design
        if self.lab_window is not None:
            try:
                self.lab_window.show()
                self.lab_window.restore()
                self.lab_window.evaluate_js(f"window.lab && lab.load({json.dumps(design)})")
                return
            except Exception:  # noqa: BLE001 - window was closed
                self.lab_window = None
        self.lab_window = webview.create_window(
            "J.A.R.V.I.S. 3D Lab", str(resource_path("ui/lab.html")), js_api=LabApi(self),
            width=1280, height=800, min_size=(800, 560), frameless=True, easy_drag=False,
            background_color="#02070c")
        self.lab_window.events.closed += lambda: setattr(self, "lab_window", None)

    def close_lab(self):
        if self.lab_window is not None:
            window, self.lab_window = self.lab_window, None
            window.destroy()

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

    # ------------------------------------------------------------ updates

    def check_updates(self, manual=False):
        try:
            self.update_info = updater.check()
        except Exception:  # noqa: BLE001 - offline / GitHub hiccup
            log.warning("update check failed", exc_info=True)
            if manual:
                self.emit("notice", {"text": "Couldn't reach the update server."})
            return
        if self.update_info:
            self.emit("update", self.update_info)
        elif manual:
            self.emit("notice", {"text": f"Jarvis v{APP_VERSION} is up to date."})

    def _update_loop(self):
        time.sleep(20)
        while True:
            self.check_updates()
            time.sleep(6 * 3600)

    def install_update(self):
        info = getattr(self, "update_info", None)
        if not info:
            return
        if not FROZEN:
            self.emit("update_progress", {"error": "Updates install from the installed app. Running from source: use git pull."})
            return
        try:
            path = updater.download(info, lambda done, total: self.emit(
                "update_progress", {"pct": done * 100 // max(total, 1)}))
            self.emit("update_progress", {"pct": 100, "installing": True})
            updater.install(path)
            time.sleep(1.5)
            self.quit()
        except Exception as e:  # noqa: BLE001
            log.exception("update failed")
            self.emit("update_progress", {"error": str(e)[:200]})

    # ------------------------------------------------------------ account

    def on_linked(self, user):
        self.emit("account", {"user": user, "required": True})
        self.announce(f"Account linked. Welcome, {user['name']}.")

    # ------------------------------------------------------------ settings

    def save_settings(self, values):
        allowed = {"address_as", "model", "voice", "voice_rate", "voice_enabled", "wake_sensitivity",
                   "protected_apps", "ask_before_risky", "home_city", "start_with_windows", "whisper_model",
                   "show_overlay", "clap_to_wake", "show_on_wake", "language", "camera_index", "show_orb",
                   "game_mode", "quiet_in_calls"}
        if "camera_index" in values:
            values["camera_index"] = int(values["camera_index"] or 0)
        clean = {k: v for k, v in values.items() if k in allowed}
        if isinstance(clean.get("protected_apps"), str):
            clean["protected_apps"] = [p.strip() for p in clean["protected_apps"].split(",") if p.strip()]
        if "wake_sensitivity" in clean:
            clean["wake_sensitivity"] = max(0.1, min(0.8, float(clean["wake_sensitivity"])))
        self.settings.update(clean)
        if "start_with_windows" in clean:
            try:
                set_autostart(bool(clean["start_with_windows"]))
            except OSError:  # antivirus programs often block new apps from adding startup entries
                log.warning("autostart change blocked", exc_info=True)
                self.emit("notice", {"text": "Windows or your antivirus blocked 'Start with Windows'."})
        if "camera_index" in clean and self.toolbox.camera.running:  # switch the live view to the new camera
            self.toolbox.camera.stop()
            threading.Timer(0.5, self.toolbox.camera.start).start()

    def _locate(self):
        """Approximate location from the IP address, so Jarvis knows your city and time zone."""
        import requests
        try:
            ip = requests.get("https://ipwho.is/", timeout=8).json()
            if ip.get("city"):
                tz = (ip.get("timezone") or {}).get("id", "")
                self.brain.location = f"The user is in or near {ip['city']}, {ip.get('country', '')} (time zone {tz})."
                self.account.location = {"country": ip.get("country"), "lat": round(float(ip["latitude"])),
                                         "lon": round(float(ip["longitude"]))}
        except Exception:  # noqa: BLE001 - offline
            log.warning("location lookup failed", exc_info=True)

    # ------------------------------------------------------------ startup

    def _step(self, key, label, status, detail=""):
        self.emit("boot", {"key": key, "label": label, "status": status, "detail": detail})

    def boot(self):
        threading.Thread(target=self._pump, daemon=True).start()
        if self.ui_ready.wait(20) and not self.background:
            self.set_fullscreen(True)
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

        # Live panels first, so the HUD is alive while the AI model is still loading onto the GPU.
        threading.Thread(target=self._stats_loop, daemon=True).start()
        threading.Thread(target=self._news_loop, daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()
        threading.Thread(target=self._locate, daemon=True).start()
        threading.Thread(target=self._calls_loop, daemon=True).start()
        self.media.start()
        self._register_hotkey()

        self._step("ollama", "Neural core (Ollama)", "run")
        if self.brain.ensure_ollama():
            self._step("ollama", "Neural core (Ollama)", "ok")
            self._boot_model()
        else:
            self._step("ollama", "Neural core (Ollama)", "fail", "Install Ollama from ollama.com, then restart Jarvis")
        threading.Thread(target=self._boot_voice, daemon=True).start()

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
            if self.background:
                self.show_window()  # can't work until linked, so don't hide in the tray
            self.announce(f"Good {part}. Please link your account to activate me.")
        elif not self.background:  # started with Windows: stay quiet in the tray
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
        """Bring the HUD back (from tray, orb or minimised) without shrinking it out of fullscreen."""
        if not self.window:
            return
        self.window.show()
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            if self.fullscreen:
                self.set_fullscreen(True)
            tools_mod.bring_to_front(hwnd)

    def set_fullscreen(self, on: bool):
        """Fill the monitor's work area (taskbar stays visible) or go back to a normal window.
        Done by hand: the built-in maximise of a borderless window leaves a 25 px gap."""
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return
        old = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # real pixels
        try:
            if user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, 9)
            left, top, right, bottom = active_work_area(hwnd)
            if on:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if not self.fullscreen:
                    self._normal_rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                x, y, w, h = left, top, right - left, bottom - top
            else:
                w, h = min(1240, right - left), min(780, bottom - top)
                x, y, w, h = getattr(self, "_normal_rect", None) or (
                    left + (right - left - w) // 2, top + (bottom - top - h) // 2, w, h)
            user32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
            self.fullscreen = on
            log.info("HUD %s at %s", "fullscreen" if on else "windowed", (x, y, w, h))
        finally:
            user32.SetThreadDpiAwarenessContext(old)

    def _on_wake(self):
        """Woken by "Hey Jarvis", a double clap or the hotkey: bring the HUD up if it was hidden."""
        self.brain.warm_up_async()  # load the AI into the GPU while you're still talking
        gaming = self.settings["game_mode"] and fullscreen_app_active()
        if self.settings["show_on_wake"] and not gaming and not self.hud_shown():
            self.show_window()

    def hide_to_tray(self):
        if self.tray:
            self.window.hide()
            self.tray.notify("Still running. Say \"Hey Jarvis\" or use the tray icon.", "J.A.R.V.I.S.")
        else:
            self.quit()

    def quit(self):
        # Hard stop after 2 s whatever happens: a half-closed Jarvis keeps its files locked,
        # which breaks updates and uninstalls.
        threading.Timer(2.0, lambda: os._exit(0)).start()
        self.voice.stop_speaking()
        self.toolbox.camera.stop()
        try:
            if self.tray:
                self.tray.stop()
        finally:
            if self.window:
                self.window.destroy()
            os._exit(0)

    @staticmethod
    def tray_possible() -> bool:
        try:
            import pystray  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def run(self):
        self.window = webview.create_window(
            WINDOW_TITLE, str(resource_path("ui/index.html")), js_api=Api(self),
            width=1240, height=780, min_size=(760, 520), frameless=True, easy_drag=False,
            background_color="#03070d", hidden=self.background and self.tray_possible())
        self._start_tray()
        # http_server: pages are served from 127.0.0.1, which YouTube live embeds need (file:// is refused)
        # private_mode: no disk cache, so an updated Jarvis never shows a stale copy of its own HUD files.
        webview.start(self.boot, private_mode=True, http_server=True)

    # ------------------------------------------------------------ uninstall

    def uninstall(self, remove_models: bool):
        """Remove Jarvis completely: device link, app files, settings, memory, speech models."""
        unins = os.path.join(os.path.dirname(sys.executable), "unins000.exe")
        if not FROZEN or not os.path.exists(unins):
            return "Uninstall only works in the installed Jarvis (this copy runs from source code)."
        try:
            self.account.sign_out()
        except Exception:  # noqa: BLE001 - offline: uninstall anyway
            log.warning("sign-out during uninstall failed", exc_info=True)
        args = [unins, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"] + (["/REMOVEMODELS"] if remove_models else [])
        import subprocess
        subprocess.Popen(args, close_fds=True)
        threading.Timer(1.0, self.quit).start()
        return "ok"


def set_autostart(enabled: bool):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if FROZEN:
                cmd = f'"{sys.executable}" --background'
            else:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                cmd = f'"{pyw}" "{os.path.abspath(sys.argv[0])}" --background'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
