# j-seeker

I've been learning to DJ, and finding and downloading tracks manually is an extremely time-consuming and tedious process. j-seeker is a tool I wrote with the help of Claude to automate this process.

The app takes in Spotify playlists that you make, and downloads them to a directory you specify. Metadata is verified and the file names for your music stay consistent.


## How does the app do this?

This app is essentially a wrapper around SLSKD (which you'll have to install and run yourself). It uses the Spotify API to pull playlists from your account, search for each of the tracks on SoulSeek through SLSKD, and simply queues them for download.

## How can I do this?

### Requirements

- A Spotify Premium account, which Spotify requires for using their API
- A Soulseek account. There's no sign-up page: logging in with an unused username creates the account
- Some technical competence and a little patience

### 1. Setup SLSKD

I'm going to leave downloading and running SLSKD to you, since some people will want to run it on a server for maybe archiving music, and some other people will just run it locally to just downloads tracks to listen to or DJ with.

You can follow the instructions on [their github](https://github.com/slskd/slskd).

What's important is that you setup both j-seeker and SLSKD's configs appropriately. You need to make sure j-seeker's paths.tracks_dir is the same as SLSKD's downloads directory. And if you're using web auth for the REST API, set the slskd.api_key in j-seeker's config as well.

These are the important parts of SLSKD's config along with my preferences:
```yaml
soulseek:
  username: <your soulseek username>
  password: <your soulseek password>

directories:
  downloads: C:/Users/[my Windows user]/Music/DJLibrary/Tracks
  # or on my laptop it'd be /home/[my Linux user]/Music/DJLibrary/Tracks
  
  # j-seeker doesn't use the incomplete downloads folder, so you can leave it commented
  # incomplete: /downloads/.incomplete

# And for SLSKD's api part of the config, I like to just disable auth entirely and only allow localhost connections
# because I'm only running SLSKD on my personal desktop and laptops. But definitely enable it and use an API key
# or use a username and password if you're accepting connections from the internet
web:
  ip_address: 127.0.0.1,[::1]

  authentication:
    disabled: true # again, don't disable this if you're allowing connections outside your machine!
    
    # These two are only necessary if auth is enabled
    username: <web UI username>
    password: <web UI password>
```

I ran into an issue where SLSKD wouldn't start the SoulSeek service on it's default port (50300) on my Windows desktop (didn't have the issue on my laptop running linux). Claude says it's because Windows reserves it for something or something. So if you run into the same problem, use a different port like 5032.

Change this in SLSKD's config if you run into the same problem:
```yaml
soulseek:
  listen_port: 5032
```

Once you have SLSKD up and running, make sure to set the appropriate slskd.url in j-seeker's config as well. It will probably be http://localhost:5030 for most people.

### 2. Create a Spotify app

1. Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Add `http://127.0.0.1:8888/callback` as a Redirect URI, and select the Web API.
3. Copy the app's Client ID. The Client Secret isn't necessary.

Spotify only lets apps read playlists you own or collaborate on. To download someone else's playlist, you can just copy and paste the playlist into another one that you create, and simply use the new playlist instead. Albums work out of the box.

### 3. Install j-seeker

You'll need [Python 3.14](https://www.python.org/downloads/) or newer. Clone this repo, then install j-seeker into a virtual environment inside it:

```bash
git clone https://github.com/jawaaddd/j-seeker.git
cd j-seeker
python -m venv .venv
.venv/bin/pip install -e .        # on Windows: .venv\Scripts\pip install -e .
```

This gives you a `jseeker` command at `.venv/bin/jseeker` (`.venv\Scripts\jseeker` on Windows). The `-e` installs it "editable", so changes to the code apply on the next run without reinstalling. If you'd rather have `jseeker` work from any terminal, install it with [pipx](https://pipx.pypa.io) instead: `pipx install .`

Run `jseeker` once. It creates its config file and tells you where it is:

- Windows: `%APPDATA%\j-seeker\config.toml`
- Linux: `~/.config/j-seeker/config.toml`

Open it and fill in:

- `spotify.client_id`: the Client ID from step 2
- `paths.tracks_dir`: the same folder as SLSKD's `directories.downloads`. If these don't match, downloads finish but j-seeker never sees them, and they fail with "no file appeared".
- `paths.playlists_dir`: where the playlist files go
- `slskd.url`: SLSKD's web address, `http://localhost:5030` by default
- `slskd.api_key`: only if SLSKD's web authentication is on. Leave it empty if it's disabled.

j-seeker creates `tracks_dir` and `playlists_dir` if they don't exist yet.

### 4. Download some songs!

Assuming SLSKD is running , then:

```bash
jseeker "https://open.spotify.com/playlist/..."
jseeker "https://open.spotify.com/album/..."
```

- The first run prints a link to log in to Spotify. Open it in your browser. Later runs reuse the login.
- While it runs, the terminal shows a live table of every track: queued, searching, downloading (with progress), done or failed. Searches and downloads run at the same time (`search.max_concurrent` and `download.max_concurrent`).
- Running it again is safe. Tracks already in your library are skipped, so you can re-run a playlist after adding songs to it, or after a run gets interrupted.
- A track that's in several playlists, or on both a single and an album, is only downloaded once.
- Downloaded files are tagged with the artist, title and album from Spotify, plus a detected BPM. The key is left empty for your DJ software to analyze.

### Configuration

Everything lives in j-seeker's `config.toml` (see step 3). Each setting has a comment explaining it, but these are the ones you're most likely to change:

| Setting | What it does |
|---|---|
| `search.formats` | File formats in order of preference. Later ones are fallbacks when no earlier format is found. |
| `search.min_bitrate` | Minimum bitrate (kbps) for lossy formats like mp3 |
| `search.include_extended` | Prefer Extended Mixes over the version on Spotify |
| `search.include_remixes` | Accept remixes by other artists when the version on Spotify can't be found |
| `search.timeout_seconds` | How long each Soulseek search collects results |
| `search.max_concurrent` | Searches running at once (default 5). Raising it much can get your Soulseek account temporarily banned. |
| `download.queue_timeout_minutes` | How long to wait for a peer to start sending before trying the next file |
| `download.stall_timeout_minutes` | How long a running transfer may go without receiving data. Slow but steady downloads are never cut off. |
| `download.parallel_peers` | While one peer has you in their queue, also ask the next one (default 2). The first to start sending wins. |
| `metadata.filename_format` | File naming, e.g. `{artist} - {title}`. `{album}` and `{bpm}` work too. |
| `metadata.bpm_min`, `metadata.bpm_max` | Detected BPMs outside this range are halved or doubled into it |

### Where your files end up

```
~/Music/DJLibrary/
├── Tracks/                     every downloaded file, named "Artist - Title.ext"
│   └── .jseeker-library.json   which file belongs to which Spotify track
└── Playlists/
    └── <playlist name>.m3u8    the playlist, pointing at files in Tracks/
                                (albums are named "Artist - Album.m3u8")
```

Playlist files list the tracks in the same order as on Spotify, and use paths relative to themselves (`../Tracks/...`). So they keep working if you move `DJLibrary` or copy it to another computer, as long as `Playlists/` and `Tracks/` stay side by side. Import them into your DJ software: in Mixxx, right-click Playlists or Crates in the sidebar and choose Import. Re-running a playlist rewrites its file to match Spotify, so import it again afterwards.

Tracks that couldn't be found are listed in a report in the `reports` folder next to j-seeker's `config.toml`. So are downloads whose original tags didn't match Spotify's, so you can check that you got the right song.

Every run also writes a detailed log to the `logs` folder next to `config.toml`: each search, which files were considered and how they were classified, download state changes, and the detected BPM. Logs older than 30 days are deleted (`logging.keep_days`).

## Disclaimer

j-seeker is just a tool. What you use it to download is on you. If you end up actually performing with a track, support the starving artist behind it.
