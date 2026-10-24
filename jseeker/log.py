"""Logging: progress to the terminal, full detail to a timestamped file in logs/."""

import logging
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console

console = Console()
log = logging.getLogger("jseeker")
# Per-track messages ("Searching: ...", "Done: ..."). While the live display is up it shows the
# same information, so these then only go to the log file.
progress = log.getChild("progress")

STYLES = {logging.WARNING: "yellow", logging.ERROR: "red", logging.CRITICAL: "red"}


class ConsoleHandler(logging.Handler):
    """Prints through Rich's console, so messages appear above the live display instead of garbling it."""

    live = False  # set by display.Display while it's showing

    def emit(self, record: logging.LogRecord) -> None:
        if self.live and record.name == progress.name:
            return
        try:
            # soft_wrap leaves wrapping to the terminal, so long links stay clickable and copyable
            console.print(self.format(record), style=STYLES.get(record.levelno), markup=False, highlight=False, soft_wrap=True)
        except Exception:
            self.handleError(record)


def setup(logs_dir: Path, keep_days: int) -> Path:
    """Send INFO and above to the terminal and everything to a new log file. Returns the file's path."""
    logs_dir.mkdir(exist_ok=True)
    cutoff = time.time() - keep_days * 86400
    for old in logs_dir.glob("*.log"):
        if old.stat().st_mtime < cutoff:
            old.unlink()

    path = logs_dir / f"{datetime.now():%Y-%m-%d_%H%M%S}.log"
    terminal = ConsoleHandler()
    terminal.setLevel(logging.INFO)
    terminal.setFormatter(logging.Formatter("%(message)s"))
    file = logging.FileHandler(path, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))

    log.setLevel(logging.DEBUG)
    log.addHandler(terminal)
    log.addHandler(file)
    return path
