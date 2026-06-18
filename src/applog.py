"""Per-session file logging for the (windowed, console-less) built app.

The frozen exe is built with ``console=False`` so no terminal opens. Instead,
every run writes a timestamped log file capturing stdout, stderr, Python
``logging`` and Qt warnings, flushed on EVERY exit -- normal (``atexit``) and
crashes (``sys.excepthook`` + ``faulthandler``). Old logs are pruned so only the
most recent sessions are kept.

``install()`` is best-effort: any failure here must never stop the app starting.
"""

from __future__ import annotations

import atexit
import datetime
import faulthandler
import logging
import os
import sys
import traceback
from pathlib import Path

APP_NAME = "AnnotationWorkbench"
MAX_SESSIONS = 50

_log_path: Path | None = None


class _Tee:
    """Write to several streams at once (e.g. the log file + the real console)."""

    def __init__(self, *streams) -> None:
        self._streams = [s for s in streams if s is not None]

    def write(self, data) -> int:
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


def log_dir() -> Path:
    """The directory holding session logs (created if missing)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = (Path(base) / APP_NAME / "logs") if base else (Path.home() / f".{APP_NAME.lower()}" / "logs")
    root.mkdir(parents=True, exist_ok=True)
    return root


def current_log_path() -> Path | None:
    """The file the running session is writing to (None if logging isn't set up)."""
    return _log_path


def _prune(directory: Path, keep: int) -> None:
    logs = sorted(directory.glob("session_*.log"), key=lambda p: p.stat().st_mtime)
    for old in logs[: max(0, len(logs) - keep)]:
        try:
            old.unlink()
        except OSError:
            pass


def install() -> Path | None:
    """Redirect output to a fresh per-session log file. Returns its path or None."""
    global _log_path
    try:
        directory = log_dir()
        _prune(directory, MAX_SESSIONS - 1)  # leave room for this session -> 50 total
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = directory / f"session_{stamp}_{os.getpid()}.log"
        handle = open(path, "w", encoding="utf-8", buffering=1)  # line-buffered
    except Exception:
        return None  # never let logging setup break startup

    _log_path = path
    handle.write(f"=== Annotation Workbench session {stamp} (pid {os.getpid()}) ===\n")
    handle.flush()

    # Tee stdout/stderr to the log (keeping the real console too, if there is one).
    sys.stdout = _Tee(sys.__stdout__, handle)
    sys.stderr = _Tee(sys.__stderr__, handle)

    logging.basicConfig(
        level=logging.INFO,
        stream=handle,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        faulthandler.enable(file=handle)  # dumps a C traceback on a hard crash
    except Exception:
        pass

    try:
        from PyQt6.QtCore import qInstallMessageHandler

        def _qt_handler(_mode, _context, message) -> None:
            try:
                handle.write(f"[Qt] {message}\n")
                handle.flush()
            except Exception:
                pass

        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass

    previous_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb) -> None:
        try:
            handle.write("\n=== UNCAUGHT EXCEPTION ===\n")
            traceback.print_exception(exc_type, exc, tb, file=handle)
            handle.flush()
        except Exception:
            pass
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    @atexit.register
    def _finish() -> None:
        try:
            handle.write(f"=== session ended ({datetime.datetime.now():%Y-%m-%d %H:%M:%S}) ===\n")
            handle.flush()
            handle.close()
        except Exception:
            pass

    return path
