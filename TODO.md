# TODO

- [x] Concurrent search and downloads
  - Downloads already run in parallel (`download.max_concurrent`, default 5), but searches run one at a time, and the script stops checking on downloads while a search is in progress.
  - Run several searches at once in a separate thread or async task, so searching and download tracking don't block each other.
  - slskd accepts only one *start search* request at a time (it returns 429 otherwise), but started searches run in parallel, so only the start calls need to be queued.
  - Cap how many searches run at once (e.g. 3). Searching too fast can get the account temporarily banned by the Soulseek server.
- [ ] Rewrite codebase for better cross-platform compatibility
  - Run without Docker: bundle slskd's standalone build and have the app start/stop it and write its config.
  - Make Essentia optional (no Windows build); DJ software like Mixxx detects BPM and key on import anyway.
  - Replace config.toml/.env with a settings file the app writes.
- [ ] GUI/Native app
  - Settings screen (music folder, Soulseek login, Spotify login) and a main screen: paste a link, download, per-track status.
  - Package for Windows (e.g. PyInstaller, built by GitHub Actions).
  - Spotify Development Mode caps an app at 5 allowlisted users; others would be added to the app's allowlist and use its Client ID.
