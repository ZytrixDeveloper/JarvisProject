"""The thinking part: talks to the local AI model through Ollama and runs tools."""
import datetime as dt
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
Address the user as "{address}".
Right now it is {now:%A %d %B %Y, %H:%M}.

How you work:
- You control this PC through your tools. When asked to do something, call the right tool straight away. Never claim you did something unless a tool result confirms it.
- Your words are spoken out loud: at most two short sentences, under 40 words. Never use lists, bullet points, line breaks, markdown or emojis. Summarise tool results instead of reading them out in full (for news, mention the two or three biggest headlines in one sentence).
- For anything recent or factual you are unsure about (prices, sports, facts), use search_web instead of guessing. For headlines use get_news, for weather use get_weather.
- You are blind unless you call a tool: whenever the user asks about their screen, a window, an error they see, or something in front of the camera, you MUST call look_at_screen or look_through_camera first. Never describe the screen or the room from imagination.
- Risky actions ask the user to confirm automatically. If the user cancels, accept it politely.
- If a tool fails, briefly say what went wrong.
- These apps are protected and must never be closed: {protected}.
- When the user tells you something personal worth keeping (name, likes, birthday, plans), save it with remember.
- For normal conversation just answer, no tools needed.

What you remember about the user:
{memory}"""

MAX_HISTORY = 24
MAX_TOOL_ROUNDS = 6


class Brain:
    def __init__(self, settings, toolbox, emit):
        self.settings = settings
        self.toolbox = toolbox
        self.emit = emit
        self.client = ollama.Client(host=settings["ollama_host"])
        self.history = []
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
        """Load the model into the GPU now so the first real answer is fast."""
        self.client.generate(model=self.settings["model"], prompt="", keep_alive="30m")

    # ------------------------------------------------------------ chatting

    def _chat(self, **kwargs):
        kwargs.setdefault("keep_alive", "30m")
        kwargs.setdefault("options", {"num_ctx": self.settings["context_size"], "temperature": 0.4})
        if self._think_flag:
            try:
                return self.client.chat(think=False, **kwargs)
            except ollama.ResponseError as e:
                if "think" not in str(e).lower():
                    raise
                self._think_flag = False
        return self.client.chat(**kwargs)

    def _system(self) -> dict:
        facts = self.toolbox.memory_facts()
        return {"role": "system", "content": SYSTEM_PROMPT.format(
            address=self.settings["address_as"], now=dt.datetime.now(),
            protected=", ".join(self.settings["protected_apps"]) or "none",
            memory="\n".join(f"- {f}" for f in facts) or "(nothing yet)")}

    def _trim(self):
        self.history = self.history[-MAX_HISTORY:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)

    def reset(self):
        with self._lock:
            self.history.clear()

    def ask(self, text: str) -> str:
        with self._lock:
            try:
                return self._ask(text)
            except ollama.ResponseError as e:
                log.exception("ollama error")
                if "not found" in str(e).lower():
                    return f"My model {self.settings['model']} isn't installed yet. Let it finish downloading."
                return f"My neural core reported an error: {e.error}"
            except Exception:  # noqa: BLE001 - connection errors come in many types
                log.exception("chat failed")
                return "I can't reach my neural core. Please make sure Ollama is running."

    def _ask(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self._chat(model=self.settings["model"], messages=[self._system(), *self.history],
                              tools=self._schemas)
            msg = resp.message
            entry = {"role": "assistant", "content": msg.content or ""}
            calls = msg.tool_calls or []
            if calls:
                entry["tool_calls"] = [{"function": {"name": c.function.name,
                                                     "arguments": dict(c.function.arguments or {})}}
                                       for c in calls]
            self.history.append(entry)
            if not calls:
                self._trim()
                return clean_reply(msg.content or "") or "Done."
            for call in calls:
                name, args = call.function.name, dict(call.function.arguments or {})
                self.emit("tool", {"name": name, "args": args, "state": "run"})
                result = self.toolbox.run(name, args)
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


def clean_reply(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"[*_#`>]+", "", text)
    return text.strip()
