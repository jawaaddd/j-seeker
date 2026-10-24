"""Finds and ranks Soulseek files for a Spotify track."""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import jev as jevlib
from .config import Config
from .jev import Jev
from .log import log
from .slskd import Slskd
from .spotify import Track

LOSSY = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}
MAX_CANDIDATES = 25  # sent to Jev in a single request
MIN_SIMILARITY = 60  # fuzzy match between a file path and "artist title", 0-100
# an unlabelled file this much longer than Spotify's version is treated as the extended mix
EXTENDED_MIN_EXTRA_SECONDS = 30

# How a file relates to the requested track
CATEGORIES = {
    "exact": "The same recording as the requested track: same version and mix. A different file format, or an 'Original Mix' label when the request has no mix name, still counts.",
    "extended": "An extended or club mix of the requested version, by the same artist.",
    "remix": "A remix of the requested track by a different artist than the requested version.",
    "other_version": "Another version of the same song: radio edit, live, acoustic, instrumental, acapella, DJ mix, bootleg, cover or a different edit.",
    "wrong_track": "A different song, or it's unclear from the file name that this is the requested song.",
}

# Version suffixes Spotify writes as "Title - Suffix"
VERSION_WORDS = r"mix|remix|edit|version|dub|rework|bootleg|flip|vip|instrumental|acapella|live|remaster(?:ed)?|mono|stereo"


@dataclass
class Candidate:
    username: str
    filename: str
    size: int
    extension: str
    length: int | None
    bitrate: int | None
    free_slot: bool
    queue_length: int
    upload_speed: int
    category: str = "wrong_track"
    confidence: float = 0.0
    similarity: float = 0.0
    inferred: bool = False  # category guessed from length rather than the name

    @property
    def basename(self) -> str:
        return re.split(r"[\\/]", self.filename)[-1]


@dataclass
class Match:
    candidates: list[Candidate] = field(default_factory=list)
    query: str = ""


def find(track: Track, cfg: Config, slskd: Slskd, jev: Jev | None) -> Match:
    """Search Soulseek for a track and return acceptable files, best first."""
    for query in search_queries(track):
        responses = slskd.search(query, cfg.search_timeout)
        candidates = _prefilter(track, responses, cfg)
        # one log record per query, so lines from searches running in parallel don't interleave
        lines = [f"    {track}: query {query!r}: {len(responses)} peers responded, {len(candidates)} files passed the filters"]
        ranked = []
        if candidates:
            _classify(track, candidates, cfg, jev)
            ranked = _rank(candidates, cfg)
            for c in {(c.basename, c.extension): c for c in candidates}.values():
                lines.append(f"      {c.category:<13} {c.confidence:.2f} {_minutes(c.length):>7} {c.extension:<4} {c.basename}")
            lines.append(f"    {len(ranked)} files accepted")
        log.debug("\n".join(lines))
        if ranked:
            return Match(ranked, query)
    return Match()


def search_queries(track: Track) -> list[str]:
    """Search texts to try in order. Soulseek matches every word, so shorter is broader."""
    title = _search_words(base_title(track.title, keep_remix=True))
    artist = _search_words(track.artists[0])
    queries = [f"{artist} {title}"]
    if len(title.split()) >= 2:
        queries.append(title)
    return queries


def base_title(title: str, keep_remix: bool = False) -> str:
    """Strip featured artists, remaster notes and edit/mix suffixes from a Spotify title."""
    title = re.sub(r"\s*[(\[](?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", "", title, flags=re.I)
    suffix = re.search(rf"\s+-\s+(.*)$|\s*[(\[]([^)\]]*\b(?:{VERSION_WORDS})\b[^)\]]*)[)\]]\s*$", title, flags=re.I)
    if suffix:
        label = suffix.group(1) or suffix.group(2)
        title = title[:suffix.start()]
        if keep_remix and is_remix_label(label):
            title += f" ({label})"
    return title.strip()


def normalize_title(title: str) -> str:
    """Rewrite Spotify's "Title - Version" style as "Title (Version)" and drop remaster notes."""
    title = re.sub(r"\s*[(\[](?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", "", title, flags=re.I)
    match = re.search(r"\s+-\s+(.*)$", title)
    if match:
        label = match.group(1)
        title = title[:match.start()]
        if not re.search(r"remaster|mono|stereo|\b\d{4}\b", label, flags=re.I):
            title += f" ({label})"
    return re.sub(r"\s*[(\[][^)\]]*remaster[^)\]]*[)\]]", "", title, flags=re.I).strip()


def is_remix_label(label: str) -> bool:
    return bool(re.search(r"remix|rmx|rework|bootleg|flip|\bvip\b|\bdub\b", label, flags=re.I)) and not re.search(r"original|extended|radio", label, flags=re.I)


def version_label(path: str, category: str) -> str | None:
    """The mix name for a downloaded file, e.g. "Extended Mix" or "Fisher Remix"."""
    stem = re.split(r"[\\/]", path)[-1].rsplit(".", 1)[0]
    if category == "extended":
        found = re.search(r"[(\[]([^)\]]*\b(?:extended|club)\b[^)\]]*)[)\]]", stem, flags=re.I)
        return found.group(1).strip() if found else "Extended Mix"
    if category == "remix":
        found = re.search(rf"[(\[]([^)\]]*\b(?:{VERSION_WORDS}|rmx)\b[^)\]]*)[)\]]", stem, flags=re.I)
        return found.group(1).strip() if found else None
    return None


def _search_words(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # single characters (the "t" in "don't") make Soulseek searches miss
    return " ".join(word for word in re.sub(r"[^\w\s]", " ", text).split() if len(word) > 1)


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _prefilter(track: Track, responses: list[dict], cfg: Config) -> list[Candidate]:
    target = _normalized(f"{track.artists[0]} {base_title(track.title)}")
    candidates = []
    for response in responses:
        for file in response.get("files", []):
            extension = (file.get("extension") or file["filename"].rsplit(".", 1)[-1]).lower().lstrip(".")
            if extension not in cfg.formats or file.get("isLocked"):
                continue
            length, bitrate = file.get("length"), file.get("bitRate")
            if extension in LOSSY:
                if not bitrate and length:
                    bitrate = file["size"] * 8 // length // 1000
                if (bitrate or 0) < cfg.min_bitrate:
                    continue
            # compare against the last few path segments, which usually hold artist/album/title
            path = " ".join(re.split(r"[\\/]", file["filename"])[-3:])
            similarity = fuzz.token_set_ratio(target, _normalized(path))
            if similarity < MIN_SIMILARITY:
                continue
            candidates.append(Candidate(
                username=response["username"],
                filename=file["filename"],
                size=file["size"],
                extension=extension,
                length=length,
                bitrate=bitrate,
                free_slot=response.get("hasFreeUploadSlot", False),
                queue_length=response.get("queueLength", 0),
                upload_speed=response.get("uploadSpeed", 0),
                similarity=similarity,
            ))
    candidates.sort(key=lambda c: (-c.similarity, cfg.formats.index(c.extension), not c.free_slot, c.queue_length))
    # many peers share the same rip; one per distinct file is enough for classification
    unique, seen = [], set()
    for c in candidates:
        key = (c.basename.lower(), c.extension)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    keep = {(c.basename.lower(), c.extension) for c in unique[:MAX_CANDIDATES]}
    return [c for c in candidates if (c.basename.lower(), c.extension) in keep]


def _classify(track: Track, candidates: list[Candidate], cfg: Config, jev: Jev | None) -> None:
    """Set each candidate's category, using Jev when available and heuristics otherwise."""
    distinct = {}
    for c in candidates:
        distinct.setdefault((c.basename.lower(), c.extension), c)
    representatives = list(distinct.values())

    results = None
    if jev:
        try:
            results = _classify_with_jev(track, representatives, jev)
        except Exception as error:
            log.warning(f"    Jev unavailable ({error}); falling back to filename matching")
    if results is None:
        results = [_classify_heuristic(track, c) for c in representatives]

    by_key = {key: result for key, result in zip(distinct, results)}
    for c in candidates:
        c.category, c.confidence = by_key[(c.basename.lower(), c.extension)]
        if not c.length:
            continue
        # trust the clock over the label: an "exact" file must be about as long as Spotify's. A longer
        # cut of the same song (e.g. the album version of a radio edit) is effectively the extended mix.
        difference = c.length - track.duration_ms / 1000
        if c.category == "exact" and difference > cfg.duration_tolerance:
            c.category = "extended" if difference >= EXTENDED_MIN_EXTRA_SECONDS else "other_version"
            c.inferred = True
        elif c.category in ("exact", "extended") and difference < -cfg.duration_tolerance:
            c.category = "other_version"


def _classify_with_jev(track: Track, candidates: list[Candidate], jev: Jev) -> list[tuple[str, float]]:
    state = {
        "requested_track": {
            "artists": track.artists,
            "title": track.title,
            "album": track.album,
            "length": _minutes(track.duration_ms / 1000),
        }
    }
    questions = {
        f"file_{i}": jevlib.choice(
            f"How does this Soulseek file relate to the requested track? "
            f"Path: {'/'.join(re.split(r'[\\/]', c.filename)[-3:])} | Length: {_minutes(c.length)}",
            CATEGORIES,
        )
        for i, c in enumerate(candidates)
    }
    answers = jev.ask(state, questions)
    results = []
    for i in range(len(candidates)):
        answer = answers.get(f"file_{i}", {})
        category = answer.get("choice", "wrong_track")
        confidence = answer.get("probabilities", {}).get(category, answer.get("confidence", 0.0))
        results.append((category, confidence))
    return results


def _classify_heuristic(track: Track, c: Candidate) -> tuple[str, float]:
    stem = c.basename.rsplit(".", 1)[0]
    if fuzz.token_set_ratio(_normalized(f"{track.artists[0]} {base_title(track.title)}"), _normalized(stem)) < 80:
        return "wrong_track", 1.0

    wanted, name = _normalized(track.title), _normalized(stem)

    def unrequested(pattern):
        return re.search(pattern, name) and not re.search(pattern, wanted)

    if unrequested(r"\b(remix|rmx|rework|bootleg|flip|vip)\b"):
        return "remix", 1.0
    if unrequested(r"\b(live|acoustic|instrumental|acapella|a cappella|karaoke|cover|demo)\b"):
        return "other_version", 1.0

    # judge the file by the bracketed labels it has that the Spotify title doesn't
    category = "exact"
    for label in re.findall(r"[(\[]([^)\]]+)[)\]]", stem):
        label = _normalized(label)
        if not label or label in wanted or re.match(r"(feat|ft|featuring|with)\b", label) or label == "original mix":
            continue
        if re.search(r"\b(remix|rmx|rework|bootleg|flip|vip|dub)\b", label) or (label.endswith("mix") and label not in ("extended mix", "club mix")):
            return "remix", 1.0
        if re.search(r"\b(extended|club mix)\b", label):
            category = "extended"
        else:
            return "other_version", 1.0  # live, edits, instrumentals, "revisited"...
    return category, 1.0


def _rank(candidates: list[Candidate], cfg: Config) -> list[Candidate]:
    versions = ["extended", "exact"] if cfg.include_extended else ["exact"]
    if cfg.include_remixes:
        versions.append("remix")
    accepted = [c for c in candidates if c.category in versions and c.confidence >= cfg.jev_min_confidence]
    return sorted(accepted, key=lambda c: (
        versions.index(c.category),
        c.inferred,
        cfg.formats.index(c.extension),
        not c.free_slot,
        c.queue_length,
        -c.upload_speed,
    ))


def _minutes(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
