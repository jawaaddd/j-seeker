"""BPM detection and tag reading/writing."""

import re
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TBPM, TIT2, TKEY, TPE1
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.ogg import OggFileType

# librosa's beat tracker works on a downsampled mono signal; 22.05 kHz is its default and plenty for beats
ANALYSIS_SAMPLE_RATE = 22050
# fewer beats than this (a very short or beatless file) can't give a trustworthy tempo
MIN_BEATS = 8
# beats in the first fit, which are numbered in order; each later fit covers twice as many
START_BEATS = 32
MP4_KEY = "----:com.apple.iTunes:initialkey"
# characters that break on FAT32/exFAT USB sticks, which most DJ hardware reads
UNSAFE_FILENAME = r'[<>:"/\\|?*\x00-\x1f]'


@dataclass
class Tags:
    artist: str
    title: str
    album: str
    bpm: str | None = None
    key: str | None = None


def detect_bpm(path: Path, decimals: int, bpm_min: float, bpm_max: float) -> str | None:
    """Detect a file's BPM with librosa. None when it has too few beats to tell."""
    bpm = _tempo(path)
    if not bpm:
        return None
    while bpm < bpm_min:
        bpm *= 2
    while bpm > bpm_max:
        bpm /= 2
    return f"{bpm:.{decimals}f}" if decimals else str(round(bpm))


def _tempo(path: Path) -> float | None:
    # imported here: librosa takes seconds to import, and only finished downloads need it
    import librosa
    import numpy as np

    audio, rate = librosa.load(path, sr=ANALYSIS_SAMPLE_RATE, mono=True)
    _, beats = librosa.beat.beat_track(y=audio, sr=rate, units="time")
    if len(beats) < MIN_BEATS:
        return None
    # librosa's own tempo estimate is only accurate to about 1 BPM. Fitting a line through the beat times
    # (beat number -> time) is far more precise: the slope is the time per beat. The catch is numbering the
    # beats. Where there are no drums to follow, as in a breakdown, the tracker keeps guessing at its own
    # slightly-off tempo and can come out a beat short, which would bend the line.
    # So fit progressively: number the first beats in order, fit them, then use the fit to number twice as
    # many beats by where they fall on its grid, refit, and so on. Beats more than a tenth of a beat off the
    # grid (the guesses) are left out of each fit.
    count = min(START_BEATS, len(beats))
    seconds_per_beat, offset = np.polyfit(np.arange(count), beats[:count], 1)
    while True:
        numbers = np.round((beats - offset) / seconds_per_beat)
        on_grid = np.abs(beats - (numbers * seconds_per_beat + offset)) < seconds_per_beat / 10
        use = on_grid & (np.arange(len(beats)) < count)
        if use.sum() >= MIN_BEATS:
            seconds_per_beat, offset = np.polyfit(numbers[use], beats[use], 1)
        if count >= len(beats):
            return 60 / seconds_per_beat
        count *= 2


def read_tags(path: Path) -> dict[str, str]:
    """Existing artist/title/album tags, whatever the container format."""
    try:
        audio = mutagen.File(path)
    except mutagen.MutagenError:
        return {}
    if audio is None or not audio.tags:
        return {}
    if isinstance(audio, MP4):
        keys = {"artist": "\xa9ART", "title": "\xa9nam", "album": "\xa9alb"}
    elif isinstance(audio, (FLAC, OggFileType)):
        keys = {"artist": "artist", "title": "title", "album": "album"}
    else:
        keys = {"artist": "TPE1", "title": "TIT2", "album": "TALB"}
    tags = {}
    for name, key in keys.items():
        value = audio.tags.get(key)
        if value:
            tags[name] = str(value[0] if isinstance(value, list) else value.text[0])
    return tags


def write_tags(path: Path, tags: Tags) -> None:
    """Write the tags into the file. Without a key, any key tag the file came with is removed,
    so DJ software detects the key itself rather than trusting the uploader's."""
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError(f"Unsupported audio file: {path.name}")

    if isinstance(audio, MP4):
        audio["\xa9ART"], audio["\xa9nam"], audio["\xa9alb"] = [tags.artist], [tags.title], [tags.album]
        if tags.bpm:
            audio["tmpo"] = [round(float(tags.bpm))]
        if tags.key:
            audio[MP4_KEY] = [MP4FreeForm(tags.key.encode())]
        else:
            audio.pop(MP4_KEY, None)
    elif isinstance(audio, (FLAC, OggFileType)):
        # Vorbis comments
        if audio.tags is None:
            audio.add_tags()
        audio["ARTIST"], audio["TITLE"], audio["ALBUM"] = tags.artist, tags.title, tags.album
        if tags.bpm:
            audio["BPM"] = tags.bpm
        if tags.key:
            audio["INITIALKEY"] = tags.key
        elif "INITIALKEY" in audio:
            del audio["INITIALKEY"]
    else:
        # mp3, aiff and wav all use ID3 frames
        if audio.tags is None:
            audio.add_tags()
        frames = [TPE1(encoding=3, text=tags.artist), TIT2(encoding=3, text=tags.title), TALB(encoding=3, text=tags.album)]
        if tags.bpm:
            frames.append(TBPM(encoding=3, text=tags.bpm))
        if tags.key:
            frames.append(TKEY(encoding=3, text=tags.key))
        else:
            audio.tags.delall("TKEY")
        for frame in frames:
            audio.tags.setall(frame.FrameID, [frame])
    audio.save()


def filename(tags: Tags, fmt: str, extension: str) -> str:
    name = fmt.format(artist=tags.artist, title=tags.title, album=tags.album, bpm=tags.bpm or "", key=tags.key or "")
    return f"{safe_name(name)}.{extension}"


def safe_name(name: str) -> str:
    name = re.sub(UNSAFE_FILENAME, "", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:200]
