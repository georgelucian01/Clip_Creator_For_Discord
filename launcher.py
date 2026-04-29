"""Entry-point for the bundled .exe.

Opens the browser shortly after the server starts, then runs the Flask app
on 127.0.0.1:5000. Used by PyInstaller via clip_creator.spec.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import webbrowser

from app import app, BASE_DIR

HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}/"


def find_free_port(preferred: int) -> int:
    """If `preferred` is taken, fall back to an OS-assigned free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def open_browser_when_ready(url: str, timeout: float = 8.0) -> None:
    """Wait until the server accepts a connection, then launch the browser."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.3):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.15)
    # last-ditch: open anyway
    webbrowser.open(url)


def main() -> None:
    global PORT, URL
    PORT = find_free_port(PORT)
    URL = f"http://{HOST}:{PORT}/"

    print()
    print("  Clip Creator")
    print("  ============")
    print(f"  Data folder : {BASE_DIR}")
    print(f"  Server      : {URL}")
    print("  (Close this window to stop the server.)")
    print()
    sys.stdout.flush()

    threading.Thread(target=open_browser_when_ready, args=(URL,), daemon=True).start()

    # use_reloader=False is critical when frozen — the reloader spawns a child
    # process that PyInstaller can't handle.
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
