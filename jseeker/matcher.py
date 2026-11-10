"""Finds and ranks Soulseek files for a Spotify track."""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .config import Config
from .log import log
from .slskd import Slskd
from .spotify import Track

LOSSY = {"mp3", "m4a", "aac", "ogg", "opus", "wma"}
MAX_CANDIDATES = 25  # distinct files kept per search, the most similar first
MIN_SIMILARITY = 60  # fuzzy match between a file path and "artist title", 0-100
# an unlabelled file this much longer than Spotify's version is treated as the extended mix
EXTENDED_MIN_EXTRA_SECONDS = 30

# How a file can relate to the requested track:
#   exact          the same recording: same version and mix ("Original Mix" counts when Spotify has no mix name)
#   extended       an extended or club mix of the requested version, by the same artist
#   remix          a remix by a different artist than the requested version
#   other_version  radio edit, live, acoustic, instrumental, acapella, bootleg, cover or another edit
#   wrong_track    a different song, or the file name doesn't make clear it's the requested song

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
    similarity: float = 0.0
    inferred: bool = False  # category guessed from length rather than the name

    @property
    def basename(self) -> str:
        return re.split(r"[\\/]", self.filename)[-1]


@dataclass
class Match:
    candidates: list[Candidate] = field(default_factory=list)
    query: str = ""


def find(track: Track, cfg: Config, slskd: Slskd) -> Match:
    """Search Soulseek for a track and return acceptable files, best first."""
    for query in search_queries(track):
        responses = slskd.search(query, cfg.search_timeout)
        candidates = _prefilter(track, responses, cfg)
        # one log record per query, so lines from searches running in parallel don't interleave
        lines = [f"    {track}: query {query!r}: {len(responses)} peers responded, {len(candidates)} files passed the filters"]
        ranked = []
        if candidates:
            _classify(track, candidates, cfg)
            ranked = _rank(candidates, cfg)
            for c in {(c.basename, c.extension): c for c in candidates}.values():
                lines.append(f"      {c.category:<13} {_minutes(c.length):>7} {c.extension:<4} {c.basename}")
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


def _classify(track: Track, candidates: list[Candidate], cfg: Config) -> None:
    """Set each candidate's category from its file name, then check it against the file's length."""
    categories = {}  # many peers share the same file; classify each distinct one once
    for c in candidates:
        key = (c.basename.lower(), c.extension)
        if key not in categories:
            categories[key] = _classify_by_name(track, c)
        c.category = categories[key]
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


def _classify_by_name(track: Track, c: Candidate) -> str:
    stem = c.basename.rsplit(".", 1)[0]
    if fuzz.token_set_ratio(_normalized(f"{track.artists[0]} {base_title(track.title)}"), _normalized(stem)) < 80:
        return "wrong_track"

    wanted, name = _normalized(track.title), _normalized(stem)

    def unrequested(pattern):
        return re.search(pattern, name) and not re.search(pattern, wanted)

    if unrequested(r"\b(remix|rmx|rework|bootleg|flip|vip)\b"):
        return "remix"
    if unrequested(r"\b(live|acoustic|instrumental|acapella|a cappella|karaoke|cover|demo)\b"):
        return "other_version"

    # judge the file by the bracketed labels it has that the Spotify title doesn't
    category = "exact"
    for label in re.findall(r"[(\[]([^)\]]+)[)\]]", stem):
        label = _normalized(label)
        if not label or label in wanted or re.match(r"(feat|ft|featuring|with)\b", label) or label == "original mix":
            continue
        if re.search(r"\b(remix|rmx|rework|bootleg|flip|vip|dub)\b", label) or (label.endswith("mix") and label not in ("extended mix", "club mix")):
            return "remix"
        if re.search(r"\b(extended|club mix)\b", label):
            category = "extended"
        else:
            return "other_version"  # live, edits, instrumentals, "revisited"...
    return category


def _rank(candidates: list[Candidate], cfg: Config) -> list[Candidate]:
    versions = ["extended", "exact"] if cfg.include_extended else ["exact"]
    if cfg.include_remixes:
        versions.append("remix")
    accepted = [c for c in candidates if c.category in versions]
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
