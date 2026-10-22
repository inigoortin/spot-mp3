# Changelog

## Mac agent security update

- The agent's web stack is upgraded to fix known vulnerabilities: FastAPI 0.159.1, Starlette 1.9.0,
  uvicorn 0.54.0, anyio 4.15.1, requests 2.33.1, certifi 2026.10.19. The phone app needs no update.
- The main environment now needs Python 3.11+ and is installed with uv, because spotdl's own
  FastAPI pin has to be overridden (`requirements-overrides.txt`). Existing installs: recreate it
  with `uv venv --python 3.11 --clear .venv` and the install line from the README, then restart
  the agent.
- POST requests to the agent must send `Content-Type: application/json` (the app always does).

## Mi Música 1.12

- The connection dot on the profile circle is no longer cut off in the top bar.
- Release build signed with your own key (`android/keystore.properties`) and optimised with R8
  (APK 19 MB → 1.8 MB).

## 1.0 — first public release

- Pipeline: Spotify export (top tracks + Liked Songs + chosen playlists), YouTube Music downloads
  preferring official studio tracks, version review, lossless loudness levelling, local Essentia
  analysis, genre tagging, playlists by audio features, private upload to Spotify.
- Library cleanup with a 7-day trash and safety checks.
- Export and automatic sync for basic MP3 players (ASCII names, `.m3u`, playlist cache reset).
- Mac agent (`agent_server.py`) with secure pairing and the Mi Música Android app 1.11
  (offline mode, outbox, live progress, version review with streaming).
- Portable setup: `config.env`, `requirements*.txt`, `download_models.sh`, LaunchAgent installers.
