"""Links this PC to the user's website account (Supabase) through the browser.

Flow: the app starts a tiny server on 127.0.0.1, opens the website's link page,
the user confirms with their password there, and the page posts a fresh session
back to the app. That session belongs to this PC only (the browser keeps its own).
The refresh token is stored encrypted with Windows DPAPI.
"""
import ctypes
import html
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from ctypes import wintypes

import requests

from .config import APP_VERSION, SESSION_FILE, SUPABASE_KEY, SUPABASE_URL, WEBSITE_URL, device_name

log = logging.getLogger("jarvis.account")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


class AuthError(Exception):
    pass


class Account:
    def __init__(self, settings):
        self.settings = settings
        self.session = self._load()
        self._server = None

    @property
    def configured(self) -> bool:
        return bool(SUPABASE_URL and SUPABASE_KEY and WEBSITE_URL)

    @property
    def user(self):
        return self.session.get("user") if self.session else None

    # ------------------------------------------------------------ storage

    def _load(self):
        try:
            return json.loads(_dpapi(SESSION_FILE.read_bytes(), protect=False))
        except (OSError, ValueError):
            return None

    def _save(self):
        if self.session:
            SESSION_FILE.write_bytes(_dpapi(json.dumps(self.session).encode(), protect=True))
        else:
            SESSION_FILE.unlink(missing_ok=True)

    # ------------------------------------------------------------ supabase calls

    def _headers(self, token=None):
        h = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _accept(self, access_token: str, refresh_token: str) -> dict:
        r = requests.get(f"{SUPABASE_URL}/auth/v1/user", headers=self._headers(access_token), timeout=10)
        if r.status_code != 200:
            raise AuthError("The website sent an invalid login.")
        u = r.json()
        meta = u.get("user_metadata") or {}
        user = {"id": u["id"], "email": u.get("email", ""),
                "name": meta.get("full_name") or meta.get("name") or u.get("email", "").split("@")[0]}
        self.session = {"access_token": access_token, "refresh_token": refresh_token, "user": user}
        self._save()
        return user

    def _refresh(self):
        r = requests.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                          headers=self._headers(), json={"refresh_token": self.session["refresh_token"]},
                          timeout=10)
        if r.status_code in (400, 401, 403):
            raise AuthError(r.json().get("error_description") or r.json().get("msg") or "session expired")
        r.raise_for_status()
        data = r.json()
        self.session["access_token"] = data["access_token"]
        self.session["refresh_token"] = data["refresh_token"]
        self._save()

    def _device_rows(self):
        r = requests.get(f"{SUPABASE_URL}/rest/v1/devices", headers=self._headers(self.session["access_token"]),
                         params={"device_id": f"eq.{self.settings['device_id']}", "select": "id"}, timeout=10)
        r.raise_for_status()
        return r.json()

    def _touch_device(self):
        requests.patch(f"{SUPABASE_URL}/rest/v1/devices",
                       headers={**self._headers(self.session["access_token"]), "Prefer": "return=minimal"},
                       params={"device_id": f"eq.{self.settings['device_id']}"},
                       json={"last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "app_version": APP_VERSION}, timeout=10)

    # ------------------------------------------------------------ public api

    def restore(self):
        """Check the saved login at startup. Returns (user or None, message)."""
        if not self.configured or not self.session:
            return None, "not signed in"
        try:
            self._refresh()
            if not self._device_rows():
                self.sign_out(remote=False)
                return None, "this PC was unlinked from your account"
            self._touch_device()
            return self.user, "ok"
        except AuthError as e:
            log.info("saved session rejected: %s", e)
            self.session = None
            self._save()
            return None, "your login expired"
        except requests.RequestException:
            log.warning("offline, using cached login")
            return self.user, "offline"

    def keep_alive(self):
        """Refresh the token every 45 minutes while the app runs."""
        def loop():
            while True:
                time.sleep(45 * 60)
                if self.session:
                    try:
                        self._refresh()
                        self._touch_device()
                    except (AuthError, requests.RequestException):
                        log.warning("background refresh failed", exc_info=True)
        threading.Thread(target=loop, daemon=True).start()

    def sign_out(self, remote=True):
        if self.session and remote:
            try:
                token = self.session["access_token"]
                requests.delete(f"{SUPABASE_URL}/rest/v1/devices", headers=self._headers(token),
                                params={"device_id": f"eq.{self.settings['device_id']}"}, timeout=10)
                requests.post(f"{SUPABASE_URL}/auth/v1/logout", headers=self._headers(token), timeout=10)
            except requests.RequestException:
                pass
        self.session = None
        self._save()

    def start_link(self, on_done):
        """Open the website so the user can link this PC. Calls on_done(user) when finished."""
        if self._server:
            self._server.shutdown()
        state = secrets.token_urlsafe(24)
        account = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _page(self, code, title, body):
                page = CALLBACK_PAGE.format(title=html.escape(title), body=html.escape(body)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def do_GET(self):
                self._page(200, "Jarvis link", "Waiting for the website to finish linking this PC.")

            def do_POST(self):
                if self.path.split("?")[0] != "/callback":
                    self._page(404, "Not found", "Nothing here.")
                    return
                length = min(int(self.headers.get("Content-Length", 0)), 64_000)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
                if not secrets.compare_digest(form.get("state", ""), state):
                    self._page(400, "Link expired", "This link request is no longer valid. Start again from the Jarvis app.")
                    return
                try:
                    user = account._accept(form.get("access_token", ""), form.get("refresh_token", ""))
                except (AuthError, requests.RequestException, KeyError) as e:
                    self._page(400, "Link failed", f"Something went wrong: {e}")
                    return
                self._page(200, "Jarvis is online", f"Signed in as {user['email']}. You can close this tab and return to Jarvis.")
                threading.Thread(target=account._finish_link, args=(on_done, user), daemon=True).start()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Timer(600, self._stop_server).start()
        query = urllib.parse.urlencode({"port": port, "state": state, "device_id": self.settings["device_id"],
                                        "device_name": device_name(), "version": APP_VERSION})
        webbrowser.open(f"{WEBSITE_URL}/link.html?{query}")

    def _finish_link(self, on_done, user):
        time.sleep(0.5)
        self._stop_server()
        self._touch_device()
        on_done(user)

    def _stop_server(self):
        if self._server:
            server, self._server = self._server, None
            server.shutdown()


CALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#03080f;color:#cfefff;
font-family:Segoe UI,system-ui,sans-serif}}
.card{{text-align:center;padding:40px 48px;border:1px solid #1d4d66;border-radius:14px;background:#06121d;
box-shadow:0 0 60px #00b7ff22;max-width:440px}}
.ring{{width:84px;height:84px;margin:0 auto 22px;border-radius:50%;border:3px solid #29d3ff;
box-shadow:0 0 24px #29d3ff,inset 0 0 24px #29d3ff}}
h1{{font-size:22px;letter-spacing:.12em;text-transform:uppercase;color:#29d3ff;margin:0 0 10px}}
p{{line-height:1.5;color:#9cc7da;margin:0}}
</style></head><body><div class="card"><div class="ring"></div><h1>{title}</h1><p>{body}</p></div></body></html>"""
