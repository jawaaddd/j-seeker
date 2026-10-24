"""Downloads a Spotify playlist through slskd and writes an .m3u8 playlist file for it."""

import json
import os
import shutil
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from rapidfuzz import fuzz

from . import jev as jevlib
from . import matcher, metadata
from .config import ROOT, Config
from .display import Display
from .jev import Jev
from .log import log, progress
from .slskd import Slskd
from .spotify import Track

STAGING = ".staging"
POLL_SECONDS = 5
STATUS_SECONDS = 60  # how often to report on downloads that are still waiting


@dataclass
class Attempt:
    """One peer asked for a track's file."""
    candidate: matcher.Candidate
    started: float
    transfer_id: str | None = None
    state: str = "Requested"
    bytes: int = 0
    last_progress: float = 0.0  # when data last arrived, once the transfer is running
    percent: float = 0.0
    speed: float = 0.0


@dataclass(eq=False)
class Job:
    track: Track
    candidates: list[matcher.Candidate]
    tried: int = 0
    attempts: dict[str, Attempt] = field(default_factory=dict)  # by slskd batch ID; several while peers queue us
    failed_users: set[str] = field(default_factory=set)
    last_error: str = ""
    current: matcher.Candidate | None = None  # the file that downloaded


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
        self.name = ""
        self.tracks: list[Track] = []
        self.playlist_file: Path | None = None
        self.display: Display | None = None
        self.abandoned: set[str] = set()  # batch IDs no longer needed but not yet seen by slskd, to cancel later
        self.done: list[str] = []
        self.flagged: list[str] = []
        self.failed: list[str] = []

    def run(self, name: str, tracks: list[Track]) -> None:
        self.name, self.tracks = name, tracks
        self.cfg.playlists_dir.mkdir(parents=True, exist_ok=True)
        self.playlist_file = self.cfg.playlists_dir / f"{metadata.safe_name(name)}.m3u8"

        pending, queued = deque(), set()
        for track in tracks:
            if self.library.file(track.id):
                self.done.append(str(track))
            elif track.id not in queued:  # a track listed twice is downloaded once
                pending.append(track)
                queued.add(track.id)
        self._write_playlist()
        log.info(f"{name}: {len(tracks)} tracks, {len(self.done)} already in your library, {len(pending)} to download\n")
        self.display = Display(name, list(pending), len(self.done))

        searching: dict[Future, Track] = {}
        found: deque[Job] = deque()  # searched, waiting for a free download slot
        active: dict[str, Job] = {}
        next_poll = last_status = time.time()
        pool = ThreadPoolExecutor(max_workers=self.cfg.max_searches, thread_name_prefix="search")
        try:
            with self.display:
                while pending or searching or found or active:
                    # Search only as far ahead as results will be used soon: peers go offline,
                    # and searching too fast can get the account temporarily banned.
                    while pending and len(searching) + len(found) < self.cfg.max_searches:
                        track = pending.popleft()
                        progress.info(f"Searching: {track}")
                        self.display.set(track, "searching")
                        searching[pool.submit(matcher.find, track, self.cfg, self.slskd, self.jev)] = track

                    while found and _job_count(active) < self.cfg.max_concurrent:
                        job = found.popleft()
                        if not self._start(job, active):
                            self._fail(job.track, "every peer refused the download")

                    # sleep until the next download poll, waking early when a search finishes
                    timeout = max(0.0, next_poll - time.time())
                    if searching:
                        finished, _ = wait(searching, timeout=timeout, return_when=FIRST_COMPLETED)
                    else:
                        finished = set()
                        time.sleep(timeout)
                    for future in finished:
                        track = searching.pop(future)
                        try:
                            match = future.result()
                        except Exception as error:
                            log.debug(f"Search error for {track}", exc_info=error)
                            self._fail(track, f"search failed: {error}")
                            continue
                        if match.candidates:
                            found.append(Job(track, match.candidates))
                            self.display.set(track, "waiting", "waiting for a download slot")
                        else:
                            self._fail(track, "no acceptable file found")

                    if active and time.time() >= next_poll:
                        try:
                            self._poll(active)
                        except requests.RequestException as error:
                            log.warning(f"  Couldn't check download progress ({error}); retrying")
                        if active and time.time() - last_status >= STATUS_SECONDS:
                            self._status(active)
                            last_status = time.time()
                    if time.time() >= next_poll:
                        next_poll = time.time() + POLL_SECONDS
        finally:
            # on Ctrl-C, end in-flight searches now instead of after their timeouts
            self.slskd.stop()
            pool.shutdown(wait=False, cancel_futures=True)

        self._report(name)

    def _start(self, job: Job, active: dict[str, Job]) -> bool:
        """Ask the job's next candidate's peer for the file. Returns False when none are left to try."""
        asking = {a.candidate.username for a in job.attempts.values()}
        while job.candidates and job.tried < self.cfg.max_attempts:
            candidate = job.candidates.pop(0)
            if candidate.username in job.failed_users or candidate.username in asking:
                continue
            job.tried += 1
            batch_id = str(uuid.uuid4())
            if self.slskd.enqueue(batch_id, candidate.username, candidate.filename, candidate.size, f"{STAGING}/{batch_id}"):
                job.attempts[batch_id] = Attempt(candidate, time.time())
                active[batch_id] = job
                self._show(job)
                progress.info(f"  Downloading [{candidate.category}, {candidate.extension}] {candidate.basename} from {candidate.username}")
                log.debug(f"    batch {batch_id}, remote path {candidate.filename}")
                return True
            log.debug(f"    {candidate.username} refused the download of {candidate.filename}")
            job.failed_users.add(candidate.username)
        return False

    def _poll(self, active: dict[str, Job]) -> None:
        now = time.time()
        transfers = {t.get("batchId"): t for t in self.slskd.downloads()}
        for batch_id in list(self.abandoned):
            transfer = transfers.get(batch_id)
            if transfer:
                if "Completed" not in transfer["state"]:
                    self.slskd.cancel(transfer["username"], transfer["id"])
                shutil.rmtree(self.cfg.tracks_dir / STAGING / batch_id, ignore_errors=True)
                self.abandoned.discard(batch_id)

        for job in {id(j): j for j in active.values()}.values():
            winner = None
            for batch_id, attempt in list(job.attempts.items()):
                transfer = transfers.get(batch_id)
                if transfer:
                    attempt.transfer_id = transfer["id"]
                    if transfer["state"] != attempt.state:
                        log.debug(f"    {job.track}: {attempt.candidate.username}: {attempt.state} -> {transfer['state']}")
                        attempt.state = transfer["state"]
                    attempt.percent = transfer.get("percentComplete", 0)
                    attempt.speed = transfer.get("averageSpeed", 0)
                    received = transfer.get("bytesTransferred", 0)
                    if received > attempt.bytes or ("InProgress" in attempt.state and not attempt.last_progress):
                        attempt.bytes, attempt.last_progress = received, now

                if "Succeeded" in attempt.state:
                    winner = batch_id
                    break
                if "Completed" in attempt.state:
                    self._drop(job, batch_id, active, attempt.state.replace("Completed, ", "").lower())
                elif "InProgress" in attempt.state:
                    if now - attempt.last_progress > self.cfg.stall_timeout:
                        self._drop(job, batch_id, active, f"stalled for {self.cfg.stall_timeout / 60:g} min")
                elif now - attempt.started > self.cfg.queue_timeout:
                    self._drop(job, batch_id, active, f"didn't start within {self.cfg.queue_timeout / 60:g} min")

            if winner:
                for other in [b for b in job.attempts if b != winner]:
                    self._drop(job, other, active)
                job.current = job.attempts.pop(winner).candidate
                del active[winner]
                self._finish(job, winner)
                continue

            running = [b for b, a in job.attempts.items() if "InProgress" in a.state]
            if running:
                # one peer is sending; the others would only download a second copy
                for other in [b for b in job.attempts if b != running[0]]:
                    self._drop(job, other, active)
            elif len(job.attempts) < self.cfg.parallel_peers and any("Queued, Remotely" in a.state for a in job.attempts.values()):
                self._start(job, active)  # while a peer has us queued, ask the next one too
            if not job.attempts and not self._start(job, active):
                self._fail(job.track, f"{job.last_error or 'download failed'}, and no candidates left")
                continue
            self._show(job)

    def _drop(self, job: Job, batch_id: str, active: dict[str, Job], reason: str | None = None) -> None:
        """Stop an attempt. With a reason it failed, and its peer isn't asked again for this track;
        without one it's simply no longer needed because another peer came through."""
        attempt = job.attempts.pop(batch_id)
        del active[batch_id]
        if "Completed" not in attempt.state:
            if attempt.transfer_id:
                self.slskd.cancel(attempt.candidate.username, attempt.transfer_id)
            else:
                self.abandoned.add(batch_id)  # slskd hasn't listed it yet; cancel it once it does
        shutil.rmtree(self.cfg.tracks_dir / STAGING / batch_id, ignore_errors=True)
        if reason:
            job.failed_users.add(attempt.candidate.username)
            job.last_error = f"{attempt.candidate.username}: {reason}"
            progress.info(f"  {job.track}: {job.last_error}, trying the next file")
        else:
            log.debug(f"    {job.track}: cancelled the request to {attempt.candidate.username}, no longer needed")

    def _status(self, active: dict[str, Job]) -> None:
        jobs = {id(j): j for j in active.values()}.values()
        progress.info(f"  Waiting on {len(jobs)} download(s):")
        for job in jobs:
            for attempt in job.attempts.values():
                minutes = (time.time() - attempt.started) / 60
                state = attempt.state.replace("Queued, Remotely", "in the peer's queue").replace("InProgress", "downloading")
                progress.info(f"    {job.track}: {state.lower()} from {attempt.candidate.username} ({minutes:.0f} min)")

    def _show(self, job: Job) -> None:
        """Update the job's row in the display: the attempt that's furthest along, plus who else was asked."""
        attempts = list(job.attempts.values())
        if not attempts:
            return
        main = next((a for a in attempts if "InProgress" in a.state), attempts[0])
        detail = f"{main.candidate.extension} from {main.candidate.username}"
        others = [a.candidate.username for a in attempts if a is not main]
        if others:
            detail += f", also asked {', '.join(others)}"
        if job.tried > 1:
            detail += f", file {job.tried} of {self.cfg.max_attempts}"
        if "InProgress" in main.state:
            self.display.set(job.track, "downloading", f"{detail}, {main.speed / 1_000_000:.1f} MB/s", main.percent)
            return
        status = "in peer's queue" if "Queued, Remotely" in main.state else "requested"
        minutes = (time.time() - main.started) / 60
        self.display.set(job.track, status, f"{detail}, {minutes:.0f} of {self.cfg.queue_timeout / 60:g} min")

    def _finish(self, job: Job, batch_id: str) -> None:
        self.display.set(job.track, "processing", "tagging, detecting BPM and key")
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
            log.debug("Post-processing error", exc_info=True)
            self._fail(job.track, f"post-processing failed: {error}")
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        self.library.add(job.track, name, flagged)
        self._write_playlist()
        self.done.append(str(job.track))
        if flagged:
            self.flagged.append(f"{job.track} -> {name}")
        progress.info(f"  Done: {name}{'  (tags disagreed with Spotify; worth a listen)' if flagged else ''}")
        self.display.set(job.track, "check tags" if flagged else "done", name)

    def _process(self, path: Path, job: Job) -> tuple[str, bool]:
        """Verify, analyze, tag and rename a downloaded file. Returns its final name and whether it's flagged."""
        track, candidate = job.track, job.current
        flagged = not self._tags_match(track, metadata.read_tags(path))

        title = matcher.normalize_title(track.title)
        label = matcher.version_label(candidate.filename, candidate.category)
        if label and label.lower() not in title.lower():
            title = f"{matcher.base_title(track.title)} ({label})"

        bpm, key = metadata.analyze(path, self.cfg.key_notation, self.cfg.bpm_decimals, self.cfg.bpm_min, self.cfg.bpm_max)
        log.debug(f"    {track}: {bpm} BPM, key {key}, title {title!r}, flagged={flagged}")
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
                log.debug(f"    Jev tag check for {track}: {existing} -> {answer['same_track']['noul']:.2f}")
                return answer["same_track"]["noul"] >= 0.5
            except Exception:
                log.debug("Jev tag check failed; using fuzzy matching", exc_info=True)
        wanted = f"{track.artists[0]} {matcher.base_title(track.title)}".lower()
        return fuzz.token_set_ratio(wanted, f"{existing.get('artist', '')} {existing['title']}".lower()) >= 75

    def _write_playlist(self) -> None:
        """Write the playlist file with every track that's in the library so far, in Spotify's order.

        Paths are relative to the playlist file, so it keeps working when DJLib is moved or opened from another OS.
        """
        tracks_dir = Path(os.path.relpath(self.cfg.tracks_dir, self.cfg.playlists_dir)).as_posix()
        lines = ["#EXTM3U", f"#PLAYLIST:{self.name}"]
        for track in self.tracks:
            file = self.library.file(track.id)
            if file:
                lines += [f"#EXTINF:{track.duration_ms // 1000},{track}", f"{tracks_dir}/{file}"]
        self.playlist_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _fail(self, track: Track, reason: str) -> None:
        self.failed.append(f"{track}: {reason}")
        progress.warning(f"  Failed: {track}: {reason}")
        self.display.set(track, "failed", reason)

    def _report(self, name: str) -> None:
        log.info(f"\n{len(self.done)} linked, {len(self.flagged)} flagged, {len(self.failed)} failed.")
        if not (self.failed or self.flagged):
            return
        reports = ROOT / "reports"
        reports.mkdir(exist_ok=True)
        path = reports / f"{datetime.now():%Y-%m-%d_%H%M} {self.playlist_file.stem}.txt"
        lines = [f"Playlist: {name}", ""]
        if self.failed:
            lines += ["Failed:", *(f"  {line}" for line in self.failed), ""]
        if self.flagged:
            lines += ["Flagged (the file's original tags didn't match Spotify; check you got the right song):",
                      *(f"  {line}" for line in self.flagged)]
        path.write_text("\n".join(lines) + "\n")
        log.info(f"Details: {path}")


def _job_count(active: dict[str, Job]) -> int:
    """Tracks being downloaded; a track asked of several peers at once has several entries in active."""
    return len({id(job) for job in active.values()})
