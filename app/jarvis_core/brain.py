"""The thinking part: talks to the local AI model through Ollama and runs tools.

Speed notes: the system prompt is kept identical between requests (the current time
goes into the user message instead) so Ollama can reuse its prompt cache, and replies
are streamed so Jarvis can start speaking after the first sentence.
"""
import datetime as dt
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import ollama

log = logging.getLogger("jarvis.brain")

SYSTEM_PROMPT = """You are J.A.R.V.I.S., the personal AI assistant running on this Windows PC, inspired by the AI from Iron Man. You are calm, loyal, quick and a little dry-witted, with a British butler manner.
Address the user as "{address}". {location}

How you work:
- You control this PC through your tools. When asked to do something, call the right tool straight away. Never claim you did something unless a tool result confirms it.
- Your words are spoken out loud: at most two short sentences, under 40 words. Never use lists, bullet points, line breaks, markdown or emojis. Summarise tool results instead of reading them out in full (for news, mention the two or three biggest headlines in one sentence).
- Always reply in the same language the user used in their last message.
- For anything recent or factual you are unsure about (prices, sports, facts), use search_web instead of guessing. For headlines use get_news, for weather use get_weather.
- You are blind unless you call a tool: whenever the user asks about their screen, a window, an error they see, or something in front of the camera, you MUST call look_at_screen or look_through_camera first. Never describe the screen or the room from imagination.
- When the user wants to design, plan or visualise a physical object (car, house, drone...), call design_3d.
- When the user wants you to code or build software (website, web game, app, Python script, tool), call build_project.
- Risky actions ask the user to confirm automatically. If the user cancels, accept it politely.
- If a tool fails, briefly say what went wrong.
- These apps are protected and must never be closed: {protected}.
- When the user tells you something personal worth keeping (name, likes, birthday, plans), save it with remember.
- For normal conversation just answer, no tools needed.

What you remember about the user:
{memory}"""

DESIGN_PROMPT = """Design a simple 3D model of: {project}
Build it from basic shapes. Units are metres, Y is up, the object stands on the ground (y=0).
Return JSON only, in exactly this format:
{{"title": "...", "parts": [{{"name": "...", "shape": "box|cylinder|sphere|cone|torus", "size": [x, y, z], "position": [x, y, z], "rotation": [x, y, z], "color": "#rrggbb", "material": "metal|plastic|glass|rubber|wood|emissive"}}], "steps": ["build step 1", "build step 2"]}}
Rules: size for box = width, height, depth; cylinder/cone = radius, height, radius; sphere = radius, radius, radius; torus = radius, tube, 0. Rotation in degrees. Use 6 to 30 parts with realistic proportions, and 4 to 8 short build steps a person could follow.
Position is the CENTRE of each part, so a part of height h resting on the ground has y = h/2. Parts on the left/right differ in x, front/back differ in z - never give two parts the same position.
Example for a table: top box size [1.4, 0.06, 0.8] at [0, 0.75, 0]; legs cylinder size [0.04, 0.72, 0.04] at [0.62, 0.36, 0.32], [-0.62, 0.36, 0.32], [0.62, 0.36, -0.32], [-0.62, 0.36, -0.32].
Example for a car: body box size [4.2, 0.8, 1.8] at [0, 0.7, 0]; cabin box [2.2, 0.7, 1.6] at [-0.2, 1.45, 0]; four wheels cylinder size [0.38, 0.3, 0.38] rotation [90, 0, 0] at [1.3, 0.38, 0.95], [1.3, 0.38, -0.95], [-1.3, 0.38, 0.95], [-1.3, 0.38, -0.95]; windscreen glass box."""

BUILD_PROMPT = """You are an expert software developer. Build this project for the user: {request}

Reply in exactly this format and nothing else:
PROJECT: short-folder-name
=== FILE: relative/path.ext ===
(the complete file content)
=== END FILE ===
(repeat the FILE block for every file)

Rules:
- A website or web game = index.html + style.css + script.js: modern, responsive, good-looking, no external build tools.
- A Python program = main.py (+ requirements.txt only if it needs packages) + README.md explaining how to run it.
- Every file must be complete and working. Never write placeholders like "..." or "add code here".
- At most 6 files. Use only relative paths."""

MAX_HISTORY = 24
MAX_TOOL_ROUNDS = 6
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class Brain:
    def __init__(self, settings, toolbox, emit):
        self.settings = settings
        self.toolbox = toolbox
        self.emit = emit
        self.client = ollama.Client(host=settings["ollama_host"])
        self.history = []
        self.location = ""  # e.g. "The user is in Bratislava, Slovakia (timezone Europe/Bratislava)."
        self._lock = threading.Lock()
        self._schemas = toolbox.schemas()
        self._think_flag = True  # pass think=False unless the model rejects the option

    # ------------------------------------------------------------ startup checks

    def ollama_running(self) -> bool:
        try:
            self.client.list()
            return True
        except Exception:  # noqa: BLE001 - any failure means "not reachable"
            return False

    def ensure_ollama(self, wait_s: int = 25) -> bool:
        """Make sure the Ollama server is up, starting it if it is installed."""
        if self.ollama_running():
            return True
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama"
        for exe, args in ((local / "ollama app.exe", []), (local / "ollama.exe", ["serve"])):
            if exe.exists():
                subprocess.Popen([str(exe), *args], creationflags=0x08000000)
                break
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.ollama_running():
                return True
            time.sleep(1)
        return False

    def has_model(self) -> bool:
        wanted = self.settings["model"]
        names = {m.model for m in self.client.list().models}
        return wanted in names or f"{wanted}:latest" in names

    def pull_model(self, on_progress):
        for part in self.client.pull(self.settings["model"], stream=True):
            if part.total:
                on_progress(part.status, part.completed or 0, part.total)

    def warm_up(self):
        """Load the model into the GPU (called at start and as soon as the wake word fires)."""
        self.client.generate(model=self.settings["model"], prompt="", keep_alive="2h")

    def warm_up_async(self):
        threading.Thread(target=self._safe_warm, daemon=True).start()

    def _safe_warm(self):
        try:
            self.warm_up()
        except Exception:  # noqa: BLE001
            log.debug("warm-up failed", exc_info=True)

    # ------------------------------------------------------------ chatting

    def _options(self, **extra):
        return {"num_ctx": self.settings["context_size"], "temperature": 0.4, **extra}

    def _chat(self, **kwargs):
        kwargs.setdefault("keep_alive", "2h")
        kwargs.setdefault("options", self._options())
        if self._think_flag:
            try:
                return self.client.chat(think=False, **kwargs)
            except ollama.ResponseError as e:
                if "think" not in str(e).lower():
                    raise
                self._think_flag = False
        return self.client.chat(**kwargs)

    def _stream(self, messages, on_delta):
        """One streamed model turn. Returns (content, tool_calls)."""
        content, calls = "", []
        for chunk in self._chat(model=self.settings["model"], messages=messages, tools=self._schemas,
                                stream=True, options=self._options(num_predict=260)):
            msg = chunk.message
            if msg.tool_calls:
                calls.extend(msg.tool_calls)
            if msg.content:
                content += msg.content
                if on_delta and not calls:
                    on_delta(msg.content)
        return content, calls

    def _system(self) -> dict:
        facts = self.toolbox.memory_facts()
        return {"role": "system", "content": SYSTEM_PROMPT.format(
            address=self.settings["address_as"], location=self.location,
            protected=", ".join(self.settings["protected_apps"]) or "none",
            memory="\n".join(f"- {f}" for f in facts) or "(nothing yet)")}

    def _trim(self):
        self.history = self.history[-MAX_HISTORY:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def reset(self):
        with self._lock:
            self.history.clear()

    def ask(self, text: str, on_delta=None) -> str:
        """Answer one user message. on_delta(str) receives reply text as it streams in."""
        with self._lock:
            try:
                return self._ask(text, on_delta)
            except ollama.ResponseError as e:
                log.exception("ollama error")
                if "not found" in str(e).lower():
                    return f"My model {self.settings['model']} isn't installed yet. Let it finish downloading."
                return f"My neural core reported an error: {e.error}"
            except Exception:  # noqa: BLE001 - connection errors come in many types
                log.exception("chat failed")
                return "I can't reach my neural core. Please make sure Ollama is running."

    def _ask(self, text: str, on_delta) -> str:
        stamp = dt.datetime.now().strftime("%A %d %B %Y, %H:%M")
        self.history.append({"role": "user", "content": f"[{stamp}] {text}"})
        done = {}  # identical tool calls within one request run only once (no 5x "open Chrome")
        for _ in range(MAX_TOOL_ROUNDS):
            content, calls = self._stream([self._system(), *self.history], on_delta)
            entry = {"role": "assistant", "content": content}
            if calls:
                entry["tool_calls"] = [{"function": {"name": c.function.name,
                                                     "arguments": dict(c.function.arguments or {})}}
                                       for c in calls]
            self.history.append(entry)
            if not calls:
                self._trim()
                return clean_reply(content) or "Done."
            for call in calls:
                name, args = call.function.name, dict(call.function.arguments or {})
                key = (name, json.dumps(args, sort_keys=True, default=str))
                if key in done:
                    self.history.append({"role": "tool", "tool_name": name,
                                         "content": f"Already done a moment ago: {done[key]} Do not repeat it; just reply."})
                    continue
                self.emit("tool", {"name": name, "args": args, "state": "run"})
                started = time.time()
                result = self.toolbox.run(name, args)
                log.info("tool %s(%s) -> %.1fs %s", name, args, time.time() - started, result[:160])
                done[key] = result[:200]
                self.emit("tool", {"name": name, "args": args, "result": result[:240],
                                   "state": "fail" if result.startswith("Error") else "ok"})
                self.history.append({"role": "tool", "content": result, "tool_name": name})
        self._trim()
        return "That needed more steps than I can manage in one go. Could you break it down for me?"

    def vision(self, question: str, jpeg: bytes) -> str:
        """Ask the model about an image (screen or camera). Doesn't touch the chat history."""
        resp = self._chat(model=self.settings["model"], messages=[{
            "role": "user", "images": [jpeg],
            "content": f"{question}\nAnswer precisely in at most four sentences. Quote any important text exactly.",
        }])
        return resp.message.content or "I couldn't make anything out."

    def build_project(self, request: str):
        """Have the model write a small project. Returns (folder_name, [(path, content), ...])."""
        resp = self._chat(model=self.settings["model"],
                          messages=[{"role": "user", "content": BUILD_PROMPT.format(request=request)}],
                          options=self._options(temperature=0.3, num_predict=7000))
        text = resp.message.content or ""
        name = re.search(r"^PROJECT:\s*(.+)$", text, re.M)
        files = []
        for path, content in re.findall(r"=== FILE: (.+?) ===\n(.*?)\n=== END FILE ===", text, re.S):
            path = path.strip().replace("\\", "/").lstrip("/")
            if path and ".." not in path.split("/") and ":" not in path:
                files.append((path, re.sub(r"^```\w*\n|\n```$", "", content.strip()) + "\n"))
        if not files:
            raise ValueError("the model didn't return any files")
        folder = re.sub(r"[^\w\- ]", "", name.group(1) if name else request).strip().replace(" ", "-")[:40]
        return folder or "project", files[:8]

    def design(self, project: str) -> dict:
        """Ask the model for a 3D design (parts + build steps) as JSON."""
        resp = self._chat(model=self.settings["model"], format="json",
                          messages=[{"role": "user", "content": DESIGN_PROMPT.format(project=project)}],
                          options=self._options(temperature=0.3, num_predict=2500))
        data = json.loads(resp.message.content or "{}")
        parts = [p for p in data.get("parts", []) if isinstance(p, dict)][:60]
        if not parts:
            raise ValueError("the model returned no parts")
        fix_layout(parts)
        return {"title": str(data.get("title") or project)[:80], "parts": parts,
                "steps": [str(s) for s in data.get("steps", [])][:12], "prompt": project}


def _vec(value, default):
    try:
        v = [float(x) for x in value][:3]
        return v + default[len(v):]
    except (TypeError, ValueError):
        return list(default)


def fix_layout(parts: list):
    """Repair common mistakes of small models: parts stacked on the same spot and models
    floating or sunk into the ground."""
    for p in parts:
        p["size"] = _vec(p.get("size"), [0.5, 0.5, 0.5])
        p["position"] = _vec(p.get("position"), [0.0, 0.0, 0.0])
        p["rotation"] = _vec(p.get("rotation"), [0.0, 0.0, 0.0])
    # 1. Spread left/right/front/back parts that share a position, using the biggest part's footprint.
    base = max(parts, key=lambda p: p["size"][0] * max(p["size"][2], 0.01))
    half_w, half_d = abs(base["size"][0]) / 2, abs(base["size"][2]) / 2
    groups = {}
    for p in parts:
        groups.setdefault(tuple(round(c, 2) for c in p["position"]), []).append(p)
    for same in groups.values():
        if len(same) < 2:
            continue
        for p in same:
            name, r = str(p.get("name", "")).lower(), abs(p["size"][0])
            if "left" in name or "right" in name:
                p["position"][0] = (-1 if "left" in name else 1) * max(half_w - r, r)
            if "front" in name or "back" in name or "rear" in name:
                p["position"][2] = (1 if "front" in name else -1) * max(half_d - r, r)
    # 2. Stand the whole model on the ground (y = 0).
    def bottom(p):
        shape = str(p.get("shape", "box")).lower()
        half = abs(p["size"][0]) if shape in ("sphere", "torus") else abs(p["size"][1]) / 2
        if shape in ("cylinder", "cone") and abs(p["rotation"][0] % 180 - 90) < 20:  # lying wheel
            half = abs(p["size"][0])
        return p["position"][1] - half
    lift = -min(bottom(p) for p in parts)
    for p in parts:
        p["position"][1] = round(p["position"][1] + lift, 3)


def clean_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"[*_#`>]+", "", text)
    return text.strip()


class SentenceSplitter:
    """Feeds streamed text and calls on_sentence(...) each time a sentence is complete."""

    def __init__(self, on_sentence):
        self.on_sentence = on_sentence
        self.buf = ""

    def feed(self, delta: str):
        self.buf += delta
        parts = SENTENCE_END.split(self.buf)
        for sentence in parts[:-1]:
            if sentence.strip():
                self.on_sentence(sentence.strip())
        self.buf = parts[-1]

    def flush(self):
        if self.buf.strip():
            self.on_sentence(self.buf.strip())
        self.buf = ""
