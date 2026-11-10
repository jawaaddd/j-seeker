import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


def _home() -> Path:
    """j-seeker's folder for its config, Spotify login, logs and reports."""
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "j-seeker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "j-seeker"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "j-seeker"


HOME = _home()
CONFIG_PATH = HOME / "config.toml"


@dataclass
class Config:
    tracks_dir: Path
    playlists_dir: Path
    slskd_url: str
    slskd_api_key: str
    spotify_client_id: str
    search_timeout: int
    max_searches: int
    formats: list[str]
    min_bitrate: int
    include_extended: bool
    include_remixes: bool
    duration_tolerance: int
    queue_timeout: int
    stall_timeout: int
    parallel_peers: int
    max_attempts: int
    max_concurrent: int
    filename_format: str
    bpm_decimals: int
    bpm_min: float
    bpm_max: float
    log_keep_days: int


def load() -> Config:
    if not CONFIG_PATH.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resources.files("jseeker") / "default_config.toml") as default:
            shutil.copy(default, CONFIG_PATH)
        raise SystemExit(f"Created {CONFIG_PATH}\nFill in your Spotify Client ID, music folders and (unless slskd's authentication is off) its API key, then run again.")
    with open(CONFIG_PATH, "rb") as f:
        toml = tomllib.load(f)

    # slskd API keys are written "role=...;cidr=...;<key>"; accept that form too and keep the key
    slskd_key = toml["slskd"].get("api_key", "").split(";")[-1].strip()
    spotify_id = toml["spotify"]["client_id"].strip()
    if not spotify_id:
        raise SystemExit(f"Missing in {CONFIG_PATH}: spotify.client_id")

    paths, search, download, meta = (toml[k] for k in ("paths", "search", "download", "metadata"))
    return Config(
        tracks_dir=Path(paths["tracks_dir"]).expanduser(),
        playlists_dir=Path(paths["playlists_dir"]).expanduser(),
        slskd_url=toml["slskd"]["url"].rstrip("/"),
        slskd_api_key=slskd_key,
        spotify_client_id=spotify_id,
        search_timeout=search["timeout_seconds"],
        max_searches=search.get("max_concurrent", 3),
        formats=[f.lower().lstrip(".") for f in search["formats"]],
        min_bitrate=search["min_bitrate"],
        include_extended=search["include_extended"],
        include_remixes=search["include_remixes"],
        duration_tolerance=search["duration_tolerance_seconds"],
        queue_timeout=download["queue_timeout_minutes"] * 60,
        stall_timeout=download["stall_timeout_minutes"] * 60,
        parallel_peers=max(1, download.get("parallel_peers", 2)),
        max_attempts=download["max_attempts"],
        max_concurrent=download["max_concurrent"],
        filename_format=meta["filename_format"],
        bpm_decimals=meta["bpm_decimals"],
        bpm_min=meta["bpm_min"],
        bpm_max=meta["bpm_max"],
        log_keep_days=toml.get("logging", {}).get("keep_days", 30),
    )
