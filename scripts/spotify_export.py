"""
Exports your Spotify top tracks and Liked Songs to data/spotify_export.csv (input of download_manager.py).

Top tracks of the last 4 weeks, 6 months and all time, plus Liked Songs and the Spotify playlists
chosen in spotify_playlists.py, without duplicates. The file is rewritten on every run:
library_cleanup.py removes the MP3s that drop out of it (save a song to Liked Songs to keep it).

Login: PKCE via spotify_auth.py. The first run must be manual (python spotify_export.py) to
approve access in the browser; after that it renews itself.

Exit codes: 0 ok, 1 Spotify error, 2 login needed (runner.sh keeps the previous export and skips
the cleanup).
"""

import csv
import os
import sys

from spotify_auth import SCOPES_READ, get_client
from common import DATA_DIR

OUTPUT = os.path.join(DATA_DIR, "spotify_export.csv")
FIELDS = ["title", "artist", "album", "duration", "source", "spotify_url"]
MAX_TOP_PER_PERIOD = 250  # change this number to get more or fewer songs per period
TOP_PERIODS = {
    "short_term": "top - last 4 weeks",
    "medium_term": "top - last 6 months",
    "long_term": "top - all time",
}


def connect():
    # Without a valid login in an unattended run, exits with code 2 (the runner keeps the previous
    # export) instead of waiting for the browser
    return get_client(f"{SCOPES_READ} playlist-read-private", "spotify_export.py")   # also the chosen playlists


def to_row(track, source):
    secs = track["duration_ms"] // 1000
    return {
        "title": track["name"],
        "artist": ", ".join(a["name"] for a in track["artists"]),
        "album": track["album"]["name"],
        "duration": f"{secs // 60}:{secs % 60:02d}",
        "source": source,
        "spotify_url": track["external_urls"].get("spotify", ""),
    }


def get_top_tracks(sp):
    merged = {}  # one entry per song; "source" lists every period it appears in
    for time_range, label in TOP_PERIODS.items():
        count = 0
        offset = 0
        while count < MAX_TOP_PER_PERIOD:
            batch = min(50, MAX_TOP_PER_PERIOD - count)
            page = sp.current_user_top_tracks(limit=batch, offset=offset, time_range=time_range)
            items = page.get("items", [])
            if not items:
                break
            for t in items:
                key = (t["name"].lower(), ", ".join(a["name"] for a in t["artists"]).lower())
                if key in merged:
                    if label not in merged[key]["source"]:
                        merged[key]["source"] += f" / {label}"
                else:
                    merged[key] = to_row(t, label)
            count += len(items)
            offset += len(items)
            if not page.get("next"):
                break
    return list(merged.values())


def get_liked_songs(sp):
    rows = []
    offset = 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = page.get("items", [])
        rows += [to_row(i["track"], "liked") for i in items if i.get("track")]
        offset += len(items)
        if not items or not page.get("next"):
            break
    return rows


def save(rows):
    tmp = OUTPUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, OUTPUT)  # never leaves the CSV half written


def get_chosen_playlists(sp):
    """
    Songs of the Spotify playlists chosen for download (spotify_playlists.py).
    A playlist that no longer exists (404) is marked "unavailable" and skipped; any other error stops
    the export (otherwise its songs would vanish from the list and the cleanup would delete them).
    """
    import spotify_playlists as spl
    rows = []
    for pid, entry in spl.chosen().items():
        try:
            meta = sp.playlist(pid, fields="name")
            tracks = spl.fetch_tracks(sp, pid)
        except Exception as e:
            if spl.is_unavailable(e):
                print(f"Aviso: la playlist '{entry.get('name', pid)}' no se puede leer en Spotify "
                      f"(borrada, de otra persona o automática de Spotify); no se descarga")
                spl.record_status(pid, "unavailable")
                continue
            raise
        spl.record_tracks(pid, meta["name"], tracks)
        label = spl.SOURCE_PREFIX + meta["name"]
        rows += [{"title": t["title"], "artist": t["artist"], "album": t["album"], "duration": t["duration"],
                  "source": label, "spotify_url": t["url"]} for t in tracks]
        print(f"Playlist '{meta['name']}': {len(tracks)} canciones")
    return rows


if __name__ == "__main__":
    sp = connect()
    new = get_top_tracks(sp) + get_liked_songs(sp) + get_chosen_playlists(sp)
    if not new:
        print("Spotify returned no songs; keeping the previous spotify_export.csv")
        sys.exit(1)

    combined = {}
    for r in new:  # one row per song; "source" joins every list it appears in
        key = (r["title"].lower(), r["artist"].lower())
        if key in combined:
            if r["source"] not in combined[key]["source"]:
                combined[key]["source"] += f" / {r['source']}"
        else:
            combined[key] = r
    save(list(combined.values()))
    counts = {label: sum(label in r["source"] for r in combined.values())
              for label in [*TOP_PERIODS.values(), "liked", "playlist: "]}
    print(f"Saved {len(combined)} songs to spotify_export.csv: "
          + ", ".join(f"{n} {label}" for label, n in counts.items()))
