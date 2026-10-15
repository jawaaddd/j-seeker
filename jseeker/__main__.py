import argparse

from . import config
from .jev import Jev
from .pipeline import Pipeline
from .slskd import Slskd
from .spotify import Spotify, parse_playlist_id


def main() -> None:
    parser = argparse.ArgumentParser(prog="AddSongs.sh", description="Download a Spotify playlist through slskd.")
    parser.add_argument("playlist", help="Spotify playlist link, URI or ID")
    args = parser.parse_args()

    cfg = config.load()
    slskd = Slskd(cfg.slskd_url, cfg.slskd_api_key)
    slskd.check()
    if not cfg.tracks_dir.is_dir():
        raise SystemExit(f"tracks_dir doesn't exist: {cfg.tracks_dir}")

    jev = Jev(cfg.jev_api_key, cfg.jev_model) if cfg.jev_api_key else None
    if not jev:
        print("JEV_API_KEY isn't set; matching files by name only.\n")

    spotify = Spotify(cfg.spotify_client_id, config.ROOT / ".spotify-token.json")
    name, tracks = spotify.playlist(parse_playlist_id(args.playlist))
    Pipeline(cfg, slskd, jev).run(name, tracks)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Downloads already queued in slskd will keep going; rerun to pick them up.")
