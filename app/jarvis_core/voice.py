"""Ears and mouth: "Hey Jarvis" wake word, speech-to-text and the Jarvis voice."""
import asyncio
import logging
import os
import queue
import re
import threading
import time

import numpy as np
import sounddevice as sd

from .config import MODELS_DIR

log = logging.getLogger("jarvis.voice")

RATE = 16000
FRAME = 1280            # 80 ms, the chunk size openWakeWord expects
END_SILENCE_FRAMES = 11  # ~0.9 s of quiet ends a command

HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.", "bye.", "."}
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|proceed|go ahead|do it|confirm|affirmative|please do)\b", re.I)
NO = re.compile(r"\b(no|nope|cancel|stop|don't|do not|abort|negative|wait)\b", re.I)

VOICES = [
    ("en-GB-RyanNeural", "Ryan (British, Jarvis-like)"),
    ("en-GB-ThomasNeural", "Thomas (British)"),
    ("en-GB-SoniaNeural", "Sonia (British, female)"),
    ("en-US-GuyNeural", "Guy (American)"),
    ("en-US-AriaNeural", "Aria (American, female)"),
]

# Native male voice per language, used when Jarvis hears you speak that language.
VOICE_BY_LANG = {
    "sk": "sk-SK-LukasNeural", "cs": "cs-CZ-AntoninNeural", "de": "de-DE-ConradNeural",
    "pl": "pl-PL-MarekNeural", "hu": "hu-HU-TamasNeural", "fr": "fr-FR-HenriNeural",
    "es": "es-ES-AlvaroNeural", "it": "it-IT-DiegoNeural", "pt": "pt-BR-AntonioNeural",
    "nl": "nl-NL-MaartenNeural", "ru": "ru-RU-DmitryNeural", "uk": "uk-UA-OstapNeural",
    "tr": "tr-TR-AhmetNeural", "ja": "ja-JP-KeitaNeural", "zh": "zh-CN-YunxiNeural",
    "ko": "ko-KR-InJoonNeural", "ar": "ar-SA-HamedNeural", "hi": "hi-IN-MadhurNeural",
    "sv": "sv-SE-MattiasNeural", "ro": "ro-RO-EmilNeural", "hr": "hr-HR-SreckoNeural",
    "sl": "sl-SI-RokNeural", "sr": "sr-RS-NicholasNeural", "bg": "bg-BG-BorislavNeural",
    "el": "el-GR-NestorasNeural", "fi": "fi-FI-HarriNeural", "da": "da-DK-JeppeNeural",
    "no": "nb-NO-FinnNeural",
}
LANGUAGES = [("auto", "Automatic (the language you speak)"), ("en", "English"), ("sk", "Slovenčina"),
             ("cs", "Čeština"), ("de", "Deutsch"), ("pl", "Polski"), ("hu", "Magyar"), ("fr", "Français"),
             ("es", "Español"), ("it", "Italiano"), ("uk", "Українська"), ("ru", "Русский")]


def _rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"[*_#`>|]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Voice:
    def __init__(self, settings, on_command, emit):
        self.settings = settings
        self.on_command = on_command
        self.emit = emit
        self.mic_on = True
        self.wake_ready = False
        self.stt_ready = False
        self._q = queue.Queue(maxsize=250)
        self._stream = None
        self._wake = None
        self._stt = None
        self._noise = 250.0
        self._trigger = threading.Event()
        self._busy = threading.Event()
        self._speaking = threading.Event()
        self._stop_speech = threading.Event()
        self._speak_lock = threading.Lock()
        self._answer_req = None
        self.on_wake = None          # called when woken (wake word, clap, hotkey): e.g. show the HUD
        self.last_lang = "en"        # language detected in the last spoken command
        self._claps = []             # timestamps of recent clap-like sound spikes
        self._prev_level = 0.0

    # ------------------------------------------------------------ loading

    def start_mic(self):
        self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                      blocksize=FRAME, callback=self._on_audio)
        self._stream.start()
        threading.Thread(target=self._loop, name="voice-loop", daemon=True).start()

    def load_wake_word(self):
        import openwakeword.utils as owu
        from openwakeword.model import Model
        folder = MODELS_DIR / "wakeword"
        folder.mkdir(parents=True, exist_ok=True)

        def find(pattern):
            return next(iter(sorted(folder.glob(pattern))), None)

        if not (find("hey_jarvis*.onnx") and find("melspectrogram*.onnx") and find("embedding_model*.onnx")):
            owu.download_models(model_names=["hey_jarvis"], target_directory=str(folder))
        self._wake = Model(wakeword_models=[str(find("hey_jarvis*.onnx"))], inference_framework="onnx",
                           melspec_model_path=str(find("melspectrogram*.onnx")),
                           embedding_model_path=str(find("embedding_model*.onnx")))
        self.wake_ready = True

    def load_speech_to_text(self):
        from faster_whisper import WhisperModel
        self._stt = WhisperModel(self.settings["whisper_model"], device="cpu", compute_type="int8",
                                 download_root=str(MODELS_DIR / "whisper"),
                                 cpu_threads=max(2, (os.cpu_count() or 4) // 2))
        self.stt_ready = True

    # ------------------------------------------------------------ public controls

    def trigger(self):
        """Start listening now (mic button / hotkey). Interrupts Jarvis if he is talking."""
        if self._speaking.is_set():
            self.stop_speaking()
        self._trigger.set()

    def busy(self, value: bool):
        (self._busy.set if value else self._busy.clear)()

    def set_mic(self, on: bool):
        self.mic_on = on
        self.emit("mic", {"on": on})

    def ask_yes_no(self, timeout: float = 20):
        """Listen for a spoken yes/no. Returns True, False or None (no clear answer)."""
        if not (self.stt_ready and self.mic_on and self._stream):
            return None
        req = {"done": threading.Event(), "text": None}
        self._answer_req = req
        req["done"].wait(timeout)
        self._answer_req = None
        text = req["text"] or ""
        if NO.search(text):
            return False
        if YES.search(text):
            return True
        return None

    # ------------------------------------------------------------ microphone loop

    def _on_audio(self, indata, frames, t, status):
        try:
            self._q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def _drain(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _loop(self):
        while True:
            try:
                frame = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if self._speaking.is_set() or not self.mic_on:
                continue
            req = self._answer_req
            if req and not req["done"].is_set():
                audio = self._record(wait_s=6, max_s=6)
                req["text"] = self.transcribe(audio) if audio is not None else ""
                req["done"].set()
                continue
            if self._trigger.is_set():
                self._trigger.clear()
                self._command()
                continue
            if self._wake is None or self._busy.is_set():
                continue
            level = _rms(frame)
            scores = self._wake.predict(frame)
            threshold = min(0.9, max(0.2, 1.0 - float(self.settings["wake_sensitivity"])))
            if scores and max(scores.values()) >= threshold:
                self._wake.reset()
                self._command()
            elif self.settings["clap_to_wake"] and self._double_clap(level) and not self._gaming():
                self._command()
            else:
                self._noise = 0.97 * self._noise + 0.03 * min(level, 3000)

    def _gaming(self) -> bool:
        """Games are loud (gunshots, explosions): no clap wake-ups while one is fullscreen."""
        from .overlay import fullscreen_app_active
        return bool(self.settings["game_mode"]) and fullscreen_app_active()

    def _double_clap(self, level: float) -> bool:
        """Two short, sharp sound spikes 0.15-0.9 s apart."""
        now = time.time()
        spike = level > max(self._noise * 10, 3500) and level > self._prev_level * 4
        self._prev_level = level
        if not spike:
            return False
        self._claps = [t for t in self._claps if now - t < 0.9] + [now]
        if len(self._claps) >= 2 and now - self._claps[-2] > 0.15:
            self._claps.clear()
            return True
        return False

    def _record(self, wait_s: float = 5, max_s: float = 14):
        """Record until the speaker pauses. None if nobody spoke."""
        chunks, started, quiet, n = [], False, 0, 0
        threshold = max(self._noise * 2.5, 380)
        start = time.time()
        while True:
            try:
                frame = self._q.get(timeout=1)
            except queue.Empty:
                if time.time() - start > max_s:
                    break
                continue
            chunks.append(frame)
            level = _rms(frame)
            n += 1
            if n % 2 == 0:
                self.emit("level", {"v": round(min(1.0, level / 4000), 3)})
            if level > threshold:
                started, quiet = True, 0
            elif started:
                quiet += 1
            elapsed = time.time() - start
            if started and quiet >= END_SILENCE_FRAMES:
                break
            if not started and elapsed > wait_s:
                return None
            if elapsed > max_s:
                break
        return np.concatenate(chunks) if chunks else None

    def _command(self):
        if not self.stt_ready:
            self.emit("notice", {"text": "Speech recognition is still loading."})
            return
        self._busy.set()
        handed_off = False
        try:
            if self.on_wake:
                self.on_wake()
            self._chime()
            self.emit("state", {"state": "listening"})
            audio = self._record()
            if audio is None:
                self.emit("state", {"state": "idle"})
                return
            self.emit("state", {"state": "transcribing"})
            text = self.transcribe(audio)
            text = re.sub(r"^\W*(hey|hi|okay|ok)?[\s,]*jarvis\W*", "", text, flags=re.I).strip()
            if text.lower() in HALLUCINATIONS:
                self.emit("state", {"state": "idle"})
                return
            handed_off = True
            threading.Thread(target=self.on_command, args=(text, "voice"), daemon=True).start()
        finally:
            if not handed_off:
                self._busy.clear()

    def transcribe(self, audio: np.ndarray) -> str:
        samples = audio.astype(np.float32) / 32768.0
        lang = self.settings["language"]
        english_only = self.settings["whisper_model"].endswith(".en")
        segments, info = self._stt.transcribe(
            samples, language="en" if english_only else (None if lang == "auto" else lang),
            beam_size=1, vad_filter=True, condition_on_previous_text=False, without_timestamps=True,
            initial_prompt="Jarvis.")
        text = " ".join(s.text.strip() for s in segments).strip()
        if info.language and (info.language_probability or 0) > 0.5:
            self.last_lang = info.language
        return text

    def current_voice(self) -> str:
        lang = self.settings["language"]
        lang = self.last_lang if lang == "auto" else lang
        return VOICE_BY_LANG.get(lang, self.settings["voice"])

    def stream(self) -> "SpeechStream":
        """Speak sentences as they arrive: the next sentence is synthesised while one plays."""
        return SpeechStream(self)

    # ------------------------------------------------------------ speaking

    def _chime(self):
        t = np.linspace(0, 0.09, int(24000 * 0.09), endpoint=False)
        fade = np.minimum(1, np.minimum(t, t[::-1]) * 60)
        tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)])
        sd.play((np.tile(fade, 2) * tone * 0.18).astype(np.float32), 24000)

    def stop_speaking(self):
        self._stop_speech.set()
        sd.stop()

    def speak(self, text: str):
        text = clean_for_speech(text)
        if not text or not self.settings["voice_enabled"]:
            return
        with self._speak_lock:
            self._speaking.set()
            self._stop_speech.clear()
            try:
                try:
                    pcm, rate = self._neural_voice(text)
                except Exception:  # noqa: BLE001 - no internet etc. -> offline Windows voice
                    log.exception("edge-tts failed, using Windows voice")
                    pcm = None
                if pcm is not None:
                    self.emit("speak", {"env": _envelope(pcm, rate), "dur": round(len(pcm) / rate, 2)})
                    sd.play(pcm, rate)
                    end = time.time() + len(pcm) / rate + 0.5
                    while time.time() < end and not self._stop_speech.is_set():
                        time.sleep(0.05)
                    sd.stop()
                else:
                    self.emit("speak", {"env": [], "dur": round(len(text) / 14, 2)})
                    self._windows_voice(text)
            finally:
                self.emit("speak_end", {})
                time.sleep(0.15)
                self._drain()
                if self._wake is not None:
                    self._wake.reset()
                self._speaking.clear()

    def _neural_voice(self, text: str):
        import edge_tts
        import miniaudio

        async def fetch():
            comm = edge_tts.Communicate(text, self.current_voice(), rate=self.settings["voice_rate"])
            data = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    data.extend(chunk["data"])
            return bytes(data)

        mp3 = asyncio.run(fetch())
        if not mp3:
            raise RuntimeError("edge-tts returned no audio")
        decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
                                   nchannels=1, sample_rate=24000)
        return np.frombuffer(decoded.samples, dtype=np.int16), 24000

    def _windows_voice(self, text: str):
        import comtypes
        import pyttsx3
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        engine = pyttsx3.init()
        for v in engine.getProperty("voices"):
            if "GB" in (v.id or "") or "George" in (v.name or ""):
                engine.setProperty("voice", v.id)
                break
        engine.say(text)
        engine.runAndWait()
        engine.stop()


class SpeechStream:
    """Pipelined speech: add() sentences while the reply streams in, play() speaks them in order."""

    def __init__(self, voice: Voice):
        self.v = voice
        self.texts = queue.Queue()
        self.audio = queue.Queue(maxsize=4)
        threading.Thread(target=self._synthesise, daemon=True).start()

    def add(self, text: str):
        text = clean_for_speech(text)
        if text and self.v.settings["voice_enabled"]:
            self.texts.put(text)

    def finish(self):
        self.texts.put(None)

    def _synthesise(self):
        while (text := self.texts.get()) is not None:
            if self.v._stop_speech.is_set():
                continue
            try:
                self.audio.put((text, self.v._neural_voice(text)))
            except Exception:  # noqa: BLE001 - offline -> Windows voice
                log.warning("edge-tts failed, using Windows voice", exc_info=True)
                self.audio.put((text, None))
        self.audio.put(None)

    def play(self):
        """Blocks until everything added before finish() has been spoken (or Esc stopped it)."""
        v = self.v
        v._stop_speech.clear()
        while (item := self.audio.get()) is not None:
            if v._stop_speech.is_set():
                continue
            text, pcm = item
            with v._speak_lock:  # held per sentence only, so confirmations can still speak
                v._speaking.set()
                try:
                    if pcm is None:
                        v.emit("speak", {"env": [], "dur": round(len(text) / 14, 2)})
                        v._windows_voice(text)
                    else:
                        samples, rate = pcm
                        v.emit("speak", {"env": _envelope(samples, rate), "dur": round(len(samples) / rate, 2)})
                        sd.play(samples, rate)
                        end = time.time() + len(samples) / rate + 0.3
                        while time.time() < end and not v._stop_speech.is_set():
                            time.sleep(0.03)
                        sd.stop()
                finally:
                    v._speaking.clear()
        v.emit("speak_end", {})
        time.sleep(0.15)
        v._drain()
        if v._wake is not None:
            v._wake.reset()


def _envelope(pcm: np.ndarray, rate: int, step_s: float = 0.05) -> list:
    step = int(rate * step_s)
    n = max(1, len(pcm) // step)
    frames = pcm[: n * step].astype(np.float32).reshape(n, step)
    env = np.sqrt((frames ** 2).mean(axis=1))
    peak = float(env.max()) or 1.0
    return [round(float(v / peak), 2) for v in env]
