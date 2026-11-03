## J.A.R.V.I.S. v0.2.1

Fixes the update problem from v0.2.0 and adds Now Playing and call detection.
Installed Jarvis offers it automatically: click **Update Jarvis**.

### Fixed
- **Updates no longer break Jarvis.** Antivirus programs (Avast, 360, ...) lock every brand-new EXE for a few seconds while they scan it; the installer used to give up after 4 seconds, leaving Jarvis without `Jarvis.exe`. It now closes any running Jarvis first and waits up to 90 seconds, and explains clearly if an antivirus really blocks it.
- Jarvis always shuts down completely before an update (no locked files).
- The HUD is live immediately at start-up (stats, news, music) while the AI model is still loading.
- Uninstall now defaults to keeping the AI models, so reinstalling doesn't mean a 3 GB download.

### New
- **Now Playing**: song, artist, album art, progress and controls for Spotify, YouTube in any browser, Apple Music, VLC and more. Ask "what song is this?".
- **Call detection**: a red "IN CALL" badge when you're in a WhatsApp, Discord, Teams, Zoom, Meet or Telegram call. While you're in a call Jarvis replies on screen instead of talking into your call. Ask "am I in a call?". Jarvis never listens to or records calls.
- Jarvis.exe and the installer now carry publisher and version details.

### Install
1. Install [Ollama](https://ollama.com/download) (free, runs the AI on your PC).
2. Run **Jarvis-Setup.exe** below. Windows SmartScreen may say "unrecognised app" because Jarvis isn't code-signed yet: **More info → Run anyway**.
3. Open Jarvis and click **Sign in with browser**.
