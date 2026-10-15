"""Downloads a Spotify playlist through slskd and builds a folder of symlinks for it."""

import json
import os
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from rapidfuzz import fuzz

from . import jev as jevlib
from . import matcher, metadata
from .config import ROOT, Config
from .jev import Jev
from .slskd import Slskd
from .spotify import Track

STAGING = ".staging"
POLL_SECONDS = 5


@dataclass
class Job:
    track: Track
    candidates: list[matcher.Candidate]
    attempts: int = 0
    current: matcher.Candidate | None = None
    started: float = 0.0
    transfer_id: str | None = None
    failed_users: set[str] = field(default_factory=set)


class Library:
    """Remembers which file in tracks_dir belongs to which Spotify track."""

    def __init__(self, tracks_dir: Path):
        self.path = tracks_dir / ".jseeker-library.json"
        self.entries = json.loads(self.path.read_text()) if self.path.exists() else {}

    def file(self, track_id: str) -> str | None:
        name = self.entries.get(track_id, {}).get("file")
        return name if name and (self.path.parent / name).exists() else None

    def add(self, track: Track, name: str, flagged: bool) -> None:
        self.entries[track.id] = {"file": name, "artist": track.artist, "title": track.title, "flagged": flagged}
        self.path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False))


class Pipeline:
    def __init__(self, cfg: Config, slskd: Slskd, jev: Jev | None):
        self.cfg = cfg
        self.slskd = slskd
        self.jev = jev
        self.library = Library(cfg.tracks_dir)
        self.playlist_dir = None
        self.done: list[str] = []
        self.flagged: list[str] = []
        self.failed: list[str] = []

    def run(self, name: str, tracks: list[Track]) -> None:
        self.playlist_dir = self.cfg.playlists_dir / metadata.safe_name(name)
        self.playlist_dir.mkdir(parents=True, exist_ok=True)

        pending = deque()
        for track in tracks:
            existing = self.library.file(track.id)
            if existing:
                self._link(existing)
                self.done.append(str(track))
            else:
                pending.append(track)
        print(f"{name}: {len(tracks)} tracks, {len(tracks) - len(pending)} already in your library, {len(pending)} to download\n")

        active: dict[str, Job] = {}
        while pending or active:
            while pending and len(active) < self.cfg.max_concurrent:
                track = pending.popleft()
                print(f"Searching: {track}")
                try:
                    match = matcher.find(track, self.cfg, self.slskd, self.jev)
                except Exception as error:
                    self._fail(track, f"search failed: {error}")
                    continue
                if not match.candidates:
                    self._fail(track, "no acceptable file found")
                    continue
                job = Job(track, match.candidates)
                if not self._start(job, active):
                    self._fail(track, "every peer refused the download")

            if active:
                time.sleep(POLL_SECONDS)
                try:
                    self._poll(active)
                except requests.RequestException as error:
                    print(f"  Couldn't check download progress ({error}); retrying")

        self._report(name)

    def _start(self, job: Job, active: dict[str, Job]) -> bool:
        """Enqueue the job's next candidate. Returns False when none are left to try."""
        while job.candidates and job.attempts < self.cfg.max_attempts:
            candidate = job.candidates.pop(0)
            if candidate.username in job.failed_users:
                continue
            job.attempts += 1
            batch_id = str(uuid.uuid4())
            if self.slskd.enqueue(batch_id, candidate.username, candidate.filename, candidate.size, f"{STAGING}/{batch_id}"):
                job.current, job.started, job.transfer_id = candidate, time.time(), None
                active[batch_id] = job
                print(f"  Downloading [{candidate.category}, {candidate.extension}] {candidate.basename} from {candidate.username}")
                return True
            job.failed_users.add(candidate.username)
        return False

    def _poll(self, active: dict[str, Job]) -> None:
        transfers = {t.get("batchId"): t for t in self.slskd.downloads() if t.get("batchId") in active}
        for batch_id, job in list(active.items()):
            transfer = transfers.get(batch_id)
            state = transfer["state"] if transfer else ""
            if transfer:
                job.transfer_id = transfer["id"]

            if "Succeeded" in state:
                del active[batch_id]
                self._finish(job, batch_id)
                continue

            timed_out = time.time() - job.started > self.cfg.download_timeout
            if "Completed" in state or timed_out:
                del active[batch_id]
                if timed_out and job.transfer_id:
                    self.slskd.cancel(job.current.username, job.transfer_id)
                reason = "timed out" if timed_out else state.replace("Completed, ", "").lower()
                print(f"  {job.track}: download {reason}, trying the next file")
                job.failed_users.add(job.current.username)
                if not self._start(job, active):
                    self._fail(job.track, f"download {reason} and no candidates left")

    def _finish(self, job: Job, batch_id: str) -> None:
        staging = self.cfg.tracks_dir / STAGING / batch_id
        # slskd marks the transfer done slightly before moving the file out of incomplete/
        for _ in range(30):
            files = [f for f in staging.glob("*") if f.is_file()] if staging.exists() else []
            if files:
                break
            time.sleep(1)
        else:
            self._fail(job.track, f"download finished but no file appeared in {staging}")
            return

        try:
            name, flagged = self._process(files[0], job)
        except Exception as error:
            self._fail(job.track, f"post-processing failed: {error}")
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        self.library.add(job.track, name, flagged)
        self._link(name)
        self.done.append(str(job.track))
        if flagged:
            self.flagged.append(f"{job.track} -> {name}")
        print(f"  Done: {name}{'  (tags disagreed with Spotify; worth a listen)' if flagged else ''}")

    def _process(self, path: Path, job: Job) -> tuple[str, bool]:
        """Verify, analyze, tag and rename a downloaded file. Returns its final name and whether it's flagged."""
        track, candidate = job.track, job.current
        flagged = not self._tags_match(track, metadata.read_tags(path))

        title = matcher.normalize_title(track.title)
        label = matcher.version_label(candidate.filename, candidate.category)
        if label and label.lower() not in title.lower():
            title = f"{matcher.base_title(track.title)} ({label})"

        bpm, key = metadata.analyze(path, self.cfg.key_notation, self.cfg.bpm_decimals, self.cfg.bpm_min, self.cfg.bpm_max)
        tags = metadata.Tags(artist=track.artist, title=title, album=track.album, bpm=bpm, key=key)
        metadata.write_tags(path, tags)

        name = metadata.filename(tags, self.cfg.filename_format, path.suffix.lstrip(".").lower())
        target = self.cfg.tracks_dir / name
        counter = 2
        while target.exists():
            target = self.cfg.tracks_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        shutil.move(path, target)
        return target.name, flagged

    def _tags_match(self, track: Track, existing: dict[str, str]) -> bool:
        """Whether the file's own tags describe the requested track. Untagged files pass."""
        if not existing.get("title"):
            return True
        if self.jev:
            try:
                answer = self.jev.ask(
                    {"requested_track": {"artists": track.artists, "title": track.title, "album": track.album},
                     "file_tags": existing},
                    {"same_track": jevlib.noul(
                        "The file's tags describe the same song as the requested track. Ignore formatting, "
                        "featured artists, album differences and mix names like 'Original' or 'Extended'."
                    )},
                )
                return answer["same_track"]["noul"] >= 0.5
            except Exception:
                pass
        wanted = f"{track.artists[0]} {matcher.base_title(track.title)}".lower()
        return fuzz.token_set_ratio(wanted, f"{existing.get('artist', '')} {existing['title']}".lower()) >= 75

    def _link(self, name: str) -> None:
        link = self.playlist_dir / name
        target = os.path.relpath(self.cfg.tracks_dir / name, self.playlist_dir)
        if link.is_symlink() or link.exists():
            if os.readlink(link) == target:
                return
            link.unlink()
        link.symlink_to(target)

    def _fail(self, track: Track, reason: str) -> None:
        self.failed.append(f"{track}: {reason}")
        print(f"  Failed: {track}: {reason}")

    def _report(self, name: str) -> None:
        print(f"\n{len(self.done)} linked, {len(self.flagged)} flagged, {len(self.failed)} failed.")
        if not (self.failed or self.flagged):
            return
        reports = ROOT / "reports"
        reports.mkdir(exist_ok=True)
        path = reports / f"{datetime.now():%Y-%m-%d_%H%M} {self.playlist_dir.name}.txt"
        lines = [f"Playlist: {name}", ""]
        if self.failed:
            lines += ["Failed:", *(f"  {line}" for line in self.failed), ""]
        if self.flagged:
            lines += ["Flagged (the file's original tags didn't match Spotify; check you got the right song):",
                      *(f"  {line}" for line in self.flagged)]
        path.write_text("\n".join(lines) + "\n")
        print(f"Details: {path}")
