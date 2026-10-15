"""Audio analysis (BPM, key) and tag reading/writing."""

import re
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TBPM, TIT2, TKEY, TPE1
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.ogg import OggFileType

FLAT_TO_SHARP = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
CAMELOT = {
    ("G#", "minor"): "1A", ("D#", "minor"): "2A", ("A#", "minor"): "3A", ("F", "minor"): "4A",
    ("C", "minor"): "5A", ("G", "minor"): "6A", ("D", "minor"): "7A", ("A", "minor"): "8A",
    ("E", "minor"): "9A", ("B", "minor"): "10A", ("F#", "minor"): "11A", ("C#", "minor"): "12A",
    ("B", "major"): "1B", ("F#", "major"): "2B", ("C#", "major"): "3B", ("G#", "major"): "4B",
    ("D#", "major"): "5B", ("A#", "major"): "6B", ("F", "major"): "7B", ("C", "major"): "8B",
    ("G", "major"): "9B", ("D", "major"): "10B", ("A", "major"): "11B", ("E", "major"): "12B",
}
# characters that break on FAT32/exFAT USB sticks, which most DJ hardware reads
UNSAFE_FILENAME = r'[<>:"/\\|?*\x00-\x1f]'


@dataclass
class Tags:
    artist: str
    title: str
    album: str
    bpm: str | None = None
    key: str | None = None


def analyze(path: Path, notation: str, bpm_decimals: int, bpm_min: float, bpm_max: float) -> tuple[str, str]:
    """Detect BPM and musical key with Essentia."""
    import essentia

    essentia.log.infoActive = False
    import essentia.standard as es

    audio = es.MonoLoader(filename=str(path), sampleRate=44100)()
    bpm = es.RhythmExtractor2013(method="multifeature")(audio)[0]
    while bpm and bpm < bpm_min:
        bpm *= 2
    while bpm > bpm_max:
        bpm /= 2
    key, scale, _ = es.KeyExtractor(profileType="edma")(audio)

    key = FLAT_TO_SHARP.get(key, key)
    if notation == "camelot":
        key_text = CAMELOT[(key, scale)]
    else:
        key_text = key + ("m" if scale == "minor" else "")
    bpm_text = f"{bpm:.{bpm_decimals}f}" if bpm_decimals else str(round(bpm))
    return bpm_text, key_text


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
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError(f"Unsupported audio file: {path.name}")

    if isinstance(audio, MP4):
        audio["\xa9ART"], audio["\xa9nam"], audio["\xa9alb"] = [tags.artist], [tags.title], [tags.album]
        if tags.bpm:
            audio["tmpo"] = [round(float(tags.bpm))]
        if tags.key:
            audio["----:com.apple.iTunes:initialkey"] = [MP4FreeForm(tags.key.encode())]
    elif isinstance(audio, (FLAC, OggFileType)):
        # Vorbis comments
        if audio.tags is None:
            audio.add_tags()
        audio["ARTIST"], audio["TITLE"], audio["ALBUM"] = tags.artist, tags.title, tags.album
        if tags.bpm:
            audio["BPM"] = tags.bpm
        if tags.key:
            audio["INITIALKEY"] = tags.key
    else:
        # mp3, aiff and wav all use ID3 frames
        if audio.tags is None:
            audio.add_tags()
        frames = [TPE1(encoding=3, text=tags.artist), TIT2(encoding=3, text=tags.title), TALB(encoding=3, text=tags.album)]
        if tags.bpm:
            frames.append(TBPM(encoding=3, text=tags.bpm))
        if tags.key:
            frames.append(TKEY(encoding=3, text=tags.key))
        for frame in frames:
            audio.tags.setall(frame.FrameID, [frame])
    audio.save()


def filename(tags: Tags, fmt: str, extension: str) -> str:
    name = fmt.format(artist=tags.artist, title=tags.title, album=tags.album, bpm=tags.bpm or "", key=tags.key or "")
    return f"{safe_name(name)}.{extension}"


def safe_name(name: str) -> str:
    name = re.sub(UNSAFE_FILENAME, "", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:200]
