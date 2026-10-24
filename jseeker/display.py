"""Live terminal view of a run: a summary line and one row per track, redrawn in place."""

import threading
import time
from dataclasses import dataclass, field

from rich.console import Group, RenderableType
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .log import ConsoleHandler, console
from .spotify import Track

STYLES = {
    "queued": "dim",
    "searching": "yellow",
    "waiting": "blue",
    "requested": "cyan",
    "in peer's queue": "cyan",
    "downloading": "cyan",
    "processing": "magenta",
    "done": "green",
    "check tags": "yellow",
    "failed": "red",
}
DOWNLOADING = ("requested", "in peer's queue", "downloading", "processing")
FINISHED = ("done", "check tags", "failed")


@dataclass
class Row:
    track: Track
    status: str = "queued"
    detail: str = ""
    percent: float | None = None
    since: float = field(default_factory=time.time)


class Display:
    """Shows every track's status while a run is going. Without a terminal (e.g. output piped to a
    file) it stays off, and the per-track log messages are printed as plain lines instead."""

    def __init__(self, name: str, tracks: list[Track], in_library: int):
        self.name = name
        self.in_library = in_library
        self.rows = {track.id: Row(track) for track in tracks}
        self.finished: list[str] = []  # track IDs, most recently finished last
        self.lock = threading.Lock()  # the display redraws from its own thread
        self.live = Live(get_renderable=self._render, console=console, refresh_per_second=4, transient=True,
                         redirect_stdout=False, redirect_stderr=False)

    def __enter__(self) -> "Display":
        if console.is_terminal and self.rows:
            ConsoleHandler.live = True
            self.live.start()
        return self

    def __exit__(self, *_) -> None:
        if not self.live.is_started:
            return
        self.live.stop()
        ConsoleHandler.live = False
        # leave the full list behind, since the live view only fits what's on screen
        with self.lock:
            console.print(self._table(list(self.rows.values())))

    def set(self, track: Track, status: str, detail: str = "", percent: float | None = None) -> None:
        with self.lock:
            row = self.rows[track.id]
            if status != row.status:
                row.since = time.time()
                if status in FINISHED:
                    self.finished.append(track.id)
            row.status, row.detail, row.percent = status, detail, percent

    def _render(self) -> RenderableType:
        with self.lock:
            rows = list(self.rows.values())
            in_progress = [r for r in rows if r.status not in FINISHED and r.status != "queued"]
            finished = [self.rows[i] for i in reversed(self.finished)]
            queued = [r for r in rows if r.status == "queued"]
            # in-progress tracks first, then the latest results, then what's next
            room = max(1, console.size.height - 4)
            shown = (in_progress + finished + queued)[:room]
            parts = [self._summary(rows), self._table(shown)]
            if len(shown) < len(rows):
                parts.append(Text(f"and {len(rows) - len(shown)} more", style="dim"))
            return Group(*parts)

    def _summary(self, rows: list[Row]) -> Text:
        def count(*statuses):
            return sum(r.status in statuses for r in rows)

        counts = [
            (count("done", "check tags"), "done", "green"),
            (count(*DOWNLOADING), "downloading", "cyan"),
            (count("searching"), "searching", "yellow"),
            (count("waiting"), "waiting", "blue"),
            (count("queued"), "queued", "dim"),
            (count("failed"), "failed", "red"),
        ]
        text = Text(self.name, style="bold")
        text.append(f"   {len(rows)} to download: ")
        text.append_text(Text(", ").join(Text(f"{n} {label}", style=style) for n, label, style in counts if n))
        if self.in_library:
            text.append(f"   ({self.in_library} already in your library)", style="dim")
        return text

    def _table(self, rows: list[Row]) -> Table:
        table = Table(box=None, expand=True, show_edge=False, header_style="bold", padding=(0, 1))
        table.add_column("Track", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("Status", width=22, no_wrap=True)
        table.add_column("Details", ratio=2, no_wrap=True, overflow="ellipsis")
        for row in rows:
            table.add_row(str(row.track), self._status(row), Text(row.detail, style="dim" if row.status not in FINISHED else ""))
        return table

    def _status(self, row: Row) -> RenderableType:
        style = STYLES.get(row.status, "")
        if row.status == "downloading" and row.percent is not None:
            cell = Table.grid(padding=(0, 1))
            cell.add_row(ProgressBar(total=100, completed=row.percent, width=12, complete_style=style),
                         Text(f"{row.percent:3.0f}%", style=style))
            return cell
        if row.status == "searching":
            return Text(f"searching {time.time() - row.since:.0f}s", style=style)
        return Text(row.status, style=style)
