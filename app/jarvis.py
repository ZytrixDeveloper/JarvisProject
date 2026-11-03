"""J.A.R.V.I.S. desktop assistant: start here (python jarvis.py, or the built Jarvis.exe)."""
import ctypes
import logging
import logging.handlers
import sys

from jarvis_core.config import LOG_FILE


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
        + ([logging.StreamHandler()] if sys.stderr else []),
    )
    # Only one Jarvis at a time.
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\JarvisHUDSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: show the running Jarvis instead
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "J.A.R.V.I.S.")
        if hwnd:
            user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
            user32.SetForegroundWindow(hwnd)
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jarvis.Assistant")

    from jarvis_core.app import JarvisApp
    # --no-voice: no mic/speech.  --test: no mic/speech and no account lock (for development).
    test = "--test" in sys.argv
    app = JarvisApp(quiet=test or "--no-voice" in sys.argv, require_account=not test)
    app.background = "--background" in sys.argv  # started with Windows: stay in the tray
    if "--say" in sys.argv[:-1]:  # run one command after startup, e.g. --say "system status"
        app.autorun = sys.argv[sys.argv.index("--say") + 1]
    app.run()


if __name__ == "__main__":
    main()
