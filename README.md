# spot-mp3

A personal music pipeline for macOS: it takes your Spotify listening (top tracks and Liked Songs),
builds a local MP3 library from YouTube Music, analyses every song locally (mood, energy, tempo,
key, genre), creates playlists from those features and keeps a basic MP3 player in sync. An
Android app (**Mi Música**) controls it from your phone over the home Wi-Fi.

> **Disclaimer — use at your own risk.** This project downloads audio from YouTube through
> [spotDL](https://github.com/spotDL/spotify-downloader). Downloading copyrighted music without
> the rights holder's permission may be illegal where you live and is against YouTube's Terms of
> Service. Only use it for content you are allowed to download. It is a personal, non-commercial
> project, provided "as is", without warranty of any kind, and not affiliated with or endorsed
> by Spotify, YouTube or the Essentia project. You are solely responsible for how you use it.

Program output and the phone app's screens are in Spanish; code, comments and docs are in English.

## How it works

```
Spotify (top tracks + Liked Songs)
   │  spotify_export.py
   ▼
data/spotify_export.csv ──► library_cleanup.py   (songs you no longer listen to → 7-day trash)
   │  download_manager.py (spotDL + YouTube Music: official studio tracks first)
   ▼
MP3 Songs/ ──► version_audit.py   (swaps live takes / covers / music videos for studio tracks)
           ──► loudness.py         (evens out loudness, lossless)
           ──► essentia_analyzer.py (local audio analysis)
   ▼
data/all_songs.csv (update_csv_schema.py + enrich_spotify_metrics.py)
   │  playlist_builder.py
   ▼
Playlists/*.m3u8 ──► spotify_upload.py (private Spotify playlists)
                 ──► mp3_export.py     (MP3 Player Card/ for a basic MP3 player)
```

`scripts/runner.sh` runs the whole chain. The phone app talks to `scripts/agent_server.py`,
which runs on the Mac.

## Folder layout

| Folder | Contents |
|---|---|
| `scripts/` | All the Python scripts and `runner.sh` |
| `data/` | Generated data (`all_songs.csv`, `spotify_export.csv`, analysis results, reviews…), with `cache/` (external services, covers), `private/` (phone tokens, Spotify login) and `locks/`. Your own `artist_genres.csv` lives here too |
| `MP3 Songs/` | The music library |
| `MP3 Player Card/` | What goes on the MP3 player's card (hard links: no extra space) |
| `Papelera MP3/` | Trash for removed or replaced MP3s, emptied after 7 days |
| `Playlists/` | Generated `.m3u8` playlists and `playlists.json` |
| `models/` | Essentia models (downloaded) |
| `logs/` | Pipeline, agent and MP3-sync logs |
| `android/` | Source of the Mi Música phone app (Kotlin + Jetpack Compose) |

Everything except `scripts/`, `android/` and the templates is created on first use and never
committed (see `.gitignore`).

## Requirements

- **macOS** (the agent, notifications, `launchd` and disk detection are Mac-specific).
- **Python 3.10+** for the main environment, and **Python 3.10 exactly** for the audio analysis
  (`essentia-tensorflow` has no wheels for newer versions). [uv](https://docs.astral.sh/uv/) makes
  the second one easy.
- **ffmpeg** (spotDL can install its own copy, see below).
- A **Spotify account** and a free Spotify developer app (for the Client ID).
- Optional: an **Android phone** for the app, and **Android Studio** or the Android SDK + JDK 17
  to build it.

Python libraries (versions known to work are pinned in the files):

| File | Libraries |
|---|---|
| `requirements.txt` | spotdl, spotipy, ytmusicapi, pandas, mutagen, requests, fastapi, uvicorn, pydantic, pillow, zeroconf |
| `requirements-essentia.txt` | essentia-tensorflow, numpy |

## Setup

All commands run from the project folder unless stated otherwise.

**1. Python environments**

```bash
python3 -m venv .venv
```

```bash
.venv/bin/python -m pip install -r requirements.txt
```

```bash
.venv/bin/spotdl --download-ffmpeg
```

```bash
uv venv --python 3.10 .venv-essentia
```

```bash
uv pip install --python .venv-essentia/bin/python -r requirements-essentia.txt
```

**2. Essentia models** (~40 MB, into `models/`)

```bash
bash scripts/download_models.sh
```

**3. Spotify app.** In the [Spotify developer dashboard](https://developer.spotify.com/dashboard),
create an app with the Web API and the redirect URI `http://127.0.0.1:8888/callback`. While the
app is in development mode, only the accounts listed under *User Management* can log in (add
yours). Copy its **Client ID** (no Client Secret is needed: login uses PKCE).

**4. Settings.** Copy the template and fill in the Client ID:

```bash
cp config.example.env config.env
```

`config.env` can also point to interpreters elsewhere (`PYTHON`, `PYTHON_ESSENTIA`); by default
`runner.sh` uses `.venv/` and `.venv-essentia/` in this folder. It is never committed.

**5. Genres (optional).** Genres are guessed per artist; to set them yourself, copy the example
and add one `artist;genre` line per artist:

```bash
cp data/artist_genres.example.csv data/artist_genres.csv
```

**6. First run and Spotify login.** The first export must be run by hand: it opens the browser
to approve access, then the login renews itself.

```bash
cd scripts && ../.venv/bin/python spotify_export.py
```

```bash
scripts/runner.sh
```

The first full run downloads and analyses your whole library, which can take a while.

## Everyday use

The examples below run from `scripts/` with the main environment active
(`source ../.venv/bin/activate`), so `python` is that interpreter.

### Update the library

```bash
./runner.sh
```

Exports your top tracks (last 4 weeks, 6 months, all time) and Liked Songs, cleans up the
library, downloads new songs, checks versions, evens out loudness, analyses the new MP3s,
rebuilds `all_songs.csv` and the playlists, and refreshes `MP3 Player Card/`. Log: `logs/`.
Exit code: 0 ok, 1 a step failed (see the log), 2 everything else ran but Spotify needs a login
(run `python spotify_export.py` by hand once). Only the last 30 run logs are kept.

### Song versions (studio vs live, covers, karaoke)

Every MP3 should come from the **official studio track** on YouTube Music. Downloads look for it
first (title, artist and duration), skipping live versions and non-original ones (karaoke,
instrumental, piano, cover, remix…) unless the Spotify title asks for them. `version_audit.py`
(run by `runner.sh`) checks which video each MP3 came from and swaps music videos, live takes,
covers and user uploads for the studio track when there is one; the old MP3 goes to the trash.

When YouTube Music doesn't have Spotify's exact version, the closest one is downloaded and left
**pending review**: approve it to keep it, or remove it (the previous MP3 comes back if there was
one; otherwise the song leaves the library and is never downloaded again). From the phone
(Inicio > "Versiones para revisar", with streaming playback) or the Mac:

```bash
python version_review.py            # one by one: [p] listen, [a] approve, [q] remove
python version_review.py --list
python version_review.py --undo "Artist - Title"   # back to pending
python version_audit.py --dry-run   # report only
```

### Loudness

`loudness.py` (run by `runner.sh`) brings every MP3 to about −11 LUFS without re-encoding: like
mp3gain, it changes the gain field of each MP3 frame in 1.5 dB steps, so quality is untouched,
it works on any player and it can be undone (`--undo`; `--dry-run` only measures).

### Playlists from your MP3s

```bash
python playlist_builder.py                    # list the example playlists
python playlist_builder.py "Spanish Pop Sad"  # build one (--all = all of them)
```

Add your own to `PRESETS` in `playlist_builder.py`, or create them from the phone app:

```python
"Indie Tranquilo": {
    "criteria": {"genre": ["Spanish Indie", "Indie Pop"], "energy_group": "Low"},
    "limit": 40,
},
```

| Criterion | Values |
|---|---|
| `genre` | Spanish Pop, Spanish Indie, Spanish Urban, Spanish Rap, Spanish Rock, Catalan Pop, Latin Pop, Urbano Latino, Pop, Indie Pop, Hip Hop, Rock, Electronic… |
| `genre_family` | Pop, Indie, Urban, Rap, Rock, Electronic, Regional, Country, Classical |
| `energy_group`, `danceability_group` | Low, Medium, High (thirds of your own library) |
| `valence_group` | Dark/Sad, Neutral, Happy/Upbeat |
| `mode` | major, minor |
| `energy_min`, `bpm_exact_max`, … | numeric limits (0-100; BPM) |
| `valence: 30`, `bpm_exact: 124` | numeric target: the closest songs go first |
| `genre_not` | exclude |

Output: `Playlists/<name>.m3u8` (opens in Music or VLC). Playlists are rebuilt on every run, so
new matching songs come in by themselves. Songs you remove from a playlist in the app never come
back, and songs you add stay even if they don't match (`excluded` / `pinned` in
`Playlists/playlists.json`).

### Library cleanup

`runner.sh` runs it after each successful export: MP3s of songs that are no longer in your top
tracks or Liked Songs go to `Papelera MP3/<date>/` (emptied after 7 days) and leave
`all_songs.csv` and every playlist. **To keep a song, save it to Liked Songs.** It does nothing
if the export is older than 12 h or would remove more than 40 % of the library at once
(`--force` skips these checks).

```bash
python library_cleanup.py --dry-run   # what would be removed, nothing is touched
```

### Upload playlists to Spotify (private)

```bash
python spotify_upload.py                    # list the available playlists
python spotify_upload.py "Spanish Pop Sad"  # upload (--dry-run = test only)
```

Uploading again replaces the songs of the playlist this script created; playlists you made by
hand are never touched.

### Download your Spotify playlists

In the app (Playlists > "Playlists de mi Spotify") or from the Mac:

```bash
python spotify_playlists.py                   # list your playlists
python spotify_playlists.py --download <id>   # download it (next ./runner.sh)
python spotify_playlists.py --stop <id>       # stop
```

Spotify only lets an app read **your own** playlists (not other people's or Spotify's automatic
ones).

### MP3 player

`MP3 Player Card/` holds every MP3 (ASCII file names, which basic players need) plus one `.m3u`
per playlist, ready for the root of the player's card. It was built for a Yoton YM03 (Actions
chip) and should work with similar players. Sync a mounted card:

```bash
python mp3_export.py --card "/Volumes/<CARD>" --dry-run   # what it would do
python mp3_export.py --card "/Volumes/<CARD>"             # copy new/changed, remove old
```

Or sync automatically whenever you plug the player in (a Mac dialog asks first):

```bash
python mp3_autosync.py --install
```

`--forget` forgets the known player and `--uninstall` removes the automation. It only touches
`.mp3`/`.m3u` files in the card's root, deletes the player's playlist cache (`M3U.LIB`) so new
playlists show up, and refuses if the export has far fewer songs than the card.

## Phone app (Android)

**On the Mac**, install the agent once; it then starts at login and keeps the Mac awake only
while a job runs:

```bash
python agent_server.py --install
```

**Build the app** with Android Studio (open `android/`) or from the command line, with
`ANDROID_HOME` and a JDK 17 set up:

```bash
cd android && ./gradlew assembleDebug
```

Install `android/app/build/outputs/apk/debug/app-debug.apk` on the phone (allow "unknown
sources" once). Then in the app: Ajustes > "Pedir código al Mac"; the code appears in a Mac
notification.

- **Jobs** (update the library, build or publish a playlist…) wait on the phone if the Mac is
  off or asleep and are sent by themselves when it is reachable.
- **Offline:** the phone keeps a copy of the last data it got; you can browse, edit and create
  playlists without the Mac, and changes are applied when it syncs.
- **Pairing security:** each device can ask for 3 codes every 10 min; 5 wrong codes block that
  device for 24 h (others can still pair). Suspicious attempts show a Mac notification. To
  unblock: `python agent_server.py --unblock` (all) or `--unblock <ip>`.
- The agent only accepts connections from the local network and serves plain HTTP with a token,
  so use it on a network you trust.

Restart the agent after changing its code:

```bash
launchctl kickstart -k gui/$(id -u)/com.spotmp3.agent
```

## Privacy

Everything personal stays on your Mac and is ignored by git: `config.env` (your Client ID),
`data/` (your library, listening data, genres, reviews, the Spotify login and phone tokens),
the music, playlists and logs. Nothing is sent anywhere except the requests to Spotify, YouTube
Music, MusicBrainz and ReccoBeats needed to build the library.

## License

[MIT](LICENSE) for this project's code. The third-party components below keep their own licenses.

## Third-party components

[spotDL](https://github.com/spotDL/spotify-downloader) (MIT),
[spotipy](https://github.com/spotipy-dev/spotipy) (MIT),
[ytmusicapi](https://github.com/sigma67/ytmusicapi) (MIT) and
[Essentia](https://essentia.upf.edu) (AGPL-3.0). The Essentia models downloaded by
`download_models.sh` are licensed by the Music Technology Group (UPF) for **non-commercial use**
(CC BY-NC-SA 4.0); see their site for terms.
