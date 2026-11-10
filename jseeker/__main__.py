import argparse
import sys

from . import config
from .log import log, setup
from .pipeline import Pipeline
from .slskd import Slskd
from .spotify import Spotify


def main() -> None:
    parser = argparse.ArgumentParser(prog="jseeker", description="Download a Spotify playlist or album through slskd.")
    parser.add_argument("link", help="Spotify playlist or album link (or URI)")
    args = parser.parse_args()

    cfg = config.load()
    log_path = setup(config.HOME / "logs", cfg.log_keep_days)
    log.debug(f"Run started for {args.link}")
    log.debug(f"Config: {cfg.__dict__ | {'slskd_api_key': '***'}}")

    try:
        slskd = Slskd(cfg.slskd_url, cfg.slskd_api_key)
        slskd.check()
        for folder in (cfg.tracks_dir, cfg.playlists_dir):
            if not folder.is_dir():
                folder.mkdir(parents=True)
                log.info(f"Created {folder}")

        spotify = Spotify(cfg.spotify_client_id, config.HOME / "spotify-token.json")
        name, tracks = spotify.fetch(args.link)
        Pipeline(cfg, slskd).run(name, tracks)
    except KeyboardInterrupt:
        log.info("\nStopped. Downloads already queued in slskd will keep going; rerun to pick them up.")
    except SystemExit as error:
        if isinstance(error.code, str):
            log.error(error.code)
            sys.exit(1)
        raise
    except Exception:
        log.exception("j-seeker crashed")
        log.info(f"Full log: {log_path}")
        sys.exit(1)
    log.info(f"Log: {log_path}")


if __name__ == "__main__":
    main()
