"""Self-update: looks for a newer Jarvis-Setup.exe on GitHub Releases and installs it."""
import hashlib
import logging
import os
import re
import subprocess
import tempfile

import requests

from .config import APP_VERSION, GITHUB_REPO

log = logging.getLogger("jarvis.updater")
LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET = "Jarvis-Setup.exe"


def _version(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def check():
    """Info about a newer release, or None if Jarvis is up to date."""
    r = requests.get(LATEST, headers={"Accept": "application/vnd.github+json", "User-Agent": "Jarvis"}, timeout=10)
    if r.status_code == 404:  # no published release yet
        return None
    r.raise_for_status()
    rel = r.json()
    if rel.get("draft") or rel.get("prerelease") or _version(rel["tag_name"]) <= _version(APP_VERSION):
        return None
    asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET), None)
    if asset is None:
        return None
    return {"version": rel["tag_name"].lstrip("v"), "current": APP_VERSION, "notes": (rel.get("body") or "")[:2000],
            "url": asset["browser_download_url"], "size": asset["size"], "digest": asset.get("digest") or ""}


def download(info: dict, on_progress) -> str:
    path = os.path.join(tempfile.gettempdir(), f"Jarvis-Setup-{info['version']}.exe")
    sha, done = hashlib.sha256(), 0
    with requests.get(info["url"], stream=True, timeout=30, headers={"User-Agent": "Jarvis"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                on_progress(done, info["size"] or done)
    if info["digest"].startswith("sha256:") and sha.hexdigest() != info["digest"].split(":", 1)[1]:
        os.remove(path)
        raise RuntimeError("The downloaded update failed its integrity check, so it was deleted.")
    return path


def install(path: str):
    """Run the installer quietly; it closes Jarvis, updates the files and starts Jarvis again."""
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], close_fds=True)
