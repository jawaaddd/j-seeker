import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (existing variables win)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    tracks_dir: Path
    playlists_dir: Path
    slskd_url: str
    slskd_api_key: str
    spotify_client_id: str
    jev_api_key: str | None
    jev_model: str
    jev_min_confidence: float
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
    key_notation: str
    bpm_decimals: int
    bpm_min: float
    bpm_max: float
    log_keep_days: int


def load() -> Config:
    load_env(ROOT / ".env")
    with open(ROOT / "config.toml", "rb") as f:
        toml = tomllib.load(f)

    # SLSKD_API_KEY is "role=...;cidr=...;<key>"; the key is the last segment
    slskd_key = os.environ.get("SLSKD_API_KEY", "").split(";")[-1]
    spotify_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    missing = [name for name, value in [("SLSKD_API_KEY", slskd_key), ("SPOTIFY_CLIENT_ID", spotify_id)] if not value]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")

    paths, search, download, meta, jev = (toml[k] for k in ("paths", "search", "download", "metadata", "jev"))
    return Config(
        tracks_dir=Path(paths["tracks_dir"]).expanduser(),
        playlists_dir=Path(paths["playlists_dir"]).expanduser(),
        slskd_url=toml["slskd"]["url"].rstrip("/"),
        slskd_api_key=slskd_key,
        spotify_client_id=spotify_id,
        jev_api_key=os.environ.get("JEV_API_KEY") or None,
        jev_model=jev["model"],
        jev_min_confidence=jev["min_confidence"],
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
        key_notation=meta["key_notation"],
        bpm_decimals=meta["bpm_decimals"],
        bpm_min=meta["bpm_min"],
        bpm_max=meta["bpm_max"],
        log_keep_days=toml.get("logging", {}).get("keep_days", 30),
    )
