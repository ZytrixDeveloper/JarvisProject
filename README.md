# J.A.R.V.I.S. — personal AI assistant for Windows

A voice-controlled assistant inspired by Iron Man's Jarvis. Say **"Hey Jarvis"** and it opens apps, manages files, plays music, reads the news, looks at your screen or webcam and answers questions — powered by an AI model that runs **on your own PC** through [Ollama](https://ollama.com).

- **Website:** https://zytrixdeveloper.github.io/JarvisProject/
- **Download:** [latest Jarvis-Setup.exe](https://github.com/ZytrixDeveloper/JarvisProject/releases/latest)

## Features (v0.2)

| | |
|---|---|
| Voice | "Hey Jarvis" wake word (openWakeWord), offline speech-to-text (faster-whisper), British neural voice (edge-tts, offline Windows voice fallback), push-to-talk `Ctrl+Alt+J` |
| Brain | Qwen3-VL via Ollama with tool calling — `4b` (fast, default) or `8b` (quality) |
| PC control | open/close apps (works with any Windows language), windows list, websites, Google, YouTube, volume, media keys, brightness, lock/sleep/shutdown, timers, typing and shortcuts, files (find/open/read/write/move/delete to Recycle Bin), PowerShell |
| Info | live news (Google News / BBC), web search (DuckDuckGo), weather (Open-Meteo / wttr.in), system status, clipboard |
| Vision | "what's on my screen?", "what am I holding?" (webcam) |
| Safety | asks before risky actions (voice "yes/no" or buttons), protected apps list (WhatsApp by default), blocks destructive commands and system folders |
| HUD | frameless Iron Man–style window: animated arc reactor, live CPU/RAM/GPU gauges, activity log, news panel, visual feed, settings, tray icon |
| Account | sign in through the website (email or Google); each PC gets its own session, stored encrypted with Windows DPAPI |
| Speed | streamed replies spoken sentence by sentence, cache-friendly prompt, model pre-loaded on wake word |
| Languages | answers in the language you speak with a native neural voice (25+ languages), knows your city/time zone |
| On-screen | floating draggable arc-reactor orb, glowing border on every monitor while working, game mode (invisible over fullscreen apps) |
| News Hub | holographic window: headlines by topic, search, and 12 live TV channels |
| 3D Lab | "design me a ..." -> Three.js model with build plan; move/rotate/resize/explode/save parts |
| Code builder | "build me a website/game/script ..." -> complete project in Documents\Jarvis Projects |
| Updates | checks GitHub Releases every 6 h; one-click silent update; full uninstall from Settings |

## Project layout

```
app/                 desktop app (Python)
  jarvis.py          entry point  (python jarvis.py [--no-voice])
  jarvis_core/       brain, tools, voice, account, HUD bridge
  ui/                HUD (HTML/CSS/JS rendered by pywebview)
  assets/            icon + installer artwork generator
installer/jarvis.iss Inno Setup script -> dist/Jarvis-Setup.exe
website/             static site for GitHub Pages (landing, login, account, device link)
build.ps1            builds Jarvis.exe + Jarvis-Setup.exe
```

## Run from source

```powershell
cd app
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python jarvis.py
```

Requires [Ollama](https://ollama.com/download). On first start Jarvis downloads its model (`qwen3-vl:4b-instruct`, ~3.3 GB), the speech model (~0.5 GB) and the wake-word model.

## Build the installer

```powershell
winget install JRSoftware.InnoSetup
powershell -ExecutionPolicy Bypass -File build.ps1
```

Output: `dist/Jarvis-Setup.exe`.

## Ship an update

1. Bump `APP_VERSION` in `app/jarvis_core/config.py` and write `installer/release-notes.md`.
2. `powershell -ExecutionPolicy Bypass -File release.ps1` (builds and uploads a **draft** GitHub release).
3. On GitHub, open the draft and click **Publish release** (leave "pre-release" unticked). Installed copies offer the update within 6 hours.

## Website + accounts (Supabase)

1. Run `website/supabase-setup.sql` in the Supabase SQL editor (creates the `devices` table with row-level security), then `website/supabase-stats.sql` (live "online now" counter and the world map; country level only).
2. Authentication → URL Configuration: Site URL `https://zytrixdeveloper.github.io/JarvisProject/`, redirect URLs `https://zytrixdeveloper.github.io/JarvisProject/**` and `http://localhost:5500/**`.
3. Google sign-in: create an OAuth client (Web application) in Google Cloud Console with redirect URI `https://cgiqqbibzmmqoquvyzlh.supabase.co/auth/v1/callback`, then paste its client ID and secret into Supabase → Authentication → Sign In / Providers → Google. The Google button appears on the site automatically once enabled (Apple works the same way).

Only the **publishable** key is used in the app and website. Never commit the secret key.

---
Fan-made project, not affiliated with Marvel.
