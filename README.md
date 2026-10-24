# j-seeker

I've been learning to DJ, and finding and downloading tracks manually is an extremely time-consuming and tedious process. j-seeker is a tool I wrote with the help of Claude to automate this process.

The app takes in Spotify playlists that you make, and downloads them to a directory you specify. Metadata is verified and the file names for your music stay consistent.


## How does the app do this?

The app wraps around a few different APIs and slskd. First, the Spotify API is used to retrieve metadata about the tracks in the playlist you want to download, like track and artist names. Then the bulk of the work is done by slskd through the REST API it exposes. The metadata from Spotify is used as search fields for the SoulSeek search, and then those search results are compared by the decision model Jev. The best results get downloaded. Then the metadata for the downloaded song is updated by the correct fields from Spotify and Essentia (used for the BPM and key). And that's it, you now have your Spotify playlist downloaded and ready to be mixed.

## How can I do this?

### Requirements

- An x86_64 machine with [Docker](https://docs.docker.com/engine/install/) and Docker Compose. Both j-seeker and [slskd](https://github.com/slskd/slskd) run in containers, so nothing else needs installing.
- A Soulseek account. There's no sign-up page: logging in with an unused username creates the account.
- A Spotify Premium account, which Spotify requires for creating a developer app
- Optional: a [TypeSafe](https://typesafe.ai) API token for Jev. Without one, files are matched by name only, which is worse at telling an extended mix from a remix or a live version.

### 1. Start slskd

```bash
./jSeeker.sh
```

Run without a link, `jSeeker.sh` only starts slskd. It also creates `~/slskd` and `~/Music/DJLib/Tracks`, and generates an API key for slskd in `.env`. To keep your library or slskd's data somewhere else, set `MUSIC_DIR` or `SLSKD_DIR` in `.env` first (see `.env.example`). On its first run, slskd writes a default config to `~/slskd/slskd.yml`. Open it and set these values (everything in it starts commented out, so uncomment the parent keys too):

```yaml
soulseek:
  username: <your soulseek username>
  password: <your soulseek password>
directories:
  downloads: /downloads/Tracks
  incomplete: /downloads/.incomplete
web:
  authentication:
    username: <web UI username>
    password: <web UI password>
```

The paths are as seen inside the container, not on your machine: `/downloads` is `~/Music/DJLib`. Using `/home/you/...` paths here will make slskd reject the config.

Then restart slskd and check that it logged in:

```bash
./Restart.sh
docker logs slskd | grep "Logged in"
```

The web UI is at http://localhost:5030. Forward TCP port 50300 on your router to this machine, or downloads from many peers will fail.

### 2. Create a Spotify app

1. Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Add `http://127.0.0.1:8888/callback` as a Redirect URI, and select the Web API.
3. Copy the app's Client ID. The Client Secret isn't needed.

Spotify only lets apps like this read playlists you own or collaborate on. To download someone else's playlist, add its tracks to a playlist of your own and use that link. Albums don't have this restriction: any album link works.

### 3. Add your keys to `.env`

`jSeeker.sh` already created `.env` with the slskd key. Add the rest:

```bash
SPOTIFY_CLIENT_ID=<your client id>
JEV_API_KEY=<your typesafe token>   # optional
```

`.env.example` lists every variable. Copy lines from it rather than copying the whole file over `.env`, or you'll overwrite the generated slskd key.

### 4. Download a playlist or album

```bash
./jSeeker.sh "https://open.spotify.com/playlist/..."
./jSeeker.sh "https://open.spotify.com/album/..."
```

- The first run builds the j-seeker image (a few minutes), then prints a link to log in to Spotify. Open it in your browser. Later runs reuse the login.
- The code is mounted into the container, so changes to it apply on the next run. After changing `requirements.txt`, rebuild with `docker compose build`.
- While it runs, the terminal shows a live table of every track: queued, searching, downloading (with progress), done or failed. Up to 3 searches run at once, alongside the downloads (`search.max_concurrent` and `download.max_concurrent`).
- Running it again is safe. Tracks already in your library are skipped, so you can re-run a playlist after adding songs to it, or after a run gets interrupted.
- A track that's in several playlists is only downloaded once.

### Configuration

Preferences live in `config.toml`:

| Setting | What it does |
|---|---|
| `search.formats` | File formats in order of preference. Later ones are fallbacks when no earlier format is found. |
| `search.min_bitrate` | Minimum bitrate (kbps) for lossy formats like mp3 |
| `search.include_extended` | Prefer Extended Mixes over the version on Spotify |
| `search.include_remixes` | Accept remixes by other artists when the version on Spotify can't be found |
| `search.timeout_seconds` | How long each Soulseek search collects results |
| `search.max_concurrent` | Searches running at once (default 3). Raising it much can get your Soulseek account temporarily banned. |
| `download.queue_timeout_minutes` | How long to wait for a peer to start sending before trying the next file |
| `download.stall_timeout_minutes` | How long a running transfer may go without receiving data. Slow but steady downloads are never cut off. |
| `download.parallel_peers` | While one peer has you in their queue, also ask the next one (default 2). The first to start sending wins. |
| `metadata.filename_format` | File naming, e.g. `{artist} - {title}` |
| `metadata.key_notation` | `camelot` (8A) or `standard` (Am) |

### Where your files end up

```
~/Music/DJLib/
├── Tracks/                     every downloaded file, named "Artist - Title.ext"
│   └── .jseeker-library.json   which file belongs to which Spotify track
└── Playlists/
    └── <playlist name>.m3u8    the playlist, pointing at files in Tracks/
                                (albums are named "Artist - Album.m3u8")
```

Playlist files use paths relative to themselves (`../Tracks/...`), so they keep working if you move `DJLib` or copy it to another computer, as long as `Playlists/` and `Tracks/` stay side by side. Import them into your DJ software: in Mixxx, right-click Playlists or Crates in the sidebar and choose Import. Re-running a playlist rewrites its file to match Spotify, so import it again afterwards.

Tracks that couldn't be found are listed in a report in `reports/`. So are downloads whose original tags didn't match Spotify's, so you can check that you got the right song.

Every run also writes a detailed log to `logs/`: each search, which files were considered and how they were classified, download state changes, and the detected BPM and key. Logs older than 30 days are deleted (`logging.keep_days` in `config.toml`).

## Disclaimer

j-seeker is just a tool. What you use it to download is on you. If you end up actually performing with a track, support the starving artist behind it. Have fun.

