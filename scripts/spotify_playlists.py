"""
Spotify playlists (yours or followed) that you want downloaded.

- data/spotify_playlists.json keeps the chosen ones: {id: {name, local_name, tracks, status, ...}}.
- spotify_export.py adds their songs to the export (source "playlist: <name>"), so they get
  downloaded and library_cleanup.py keeps them while they stay chosen.
- playlist_builder.py builds a local playlist from each (source "spotify", same order as in
  Spotify) that respects what you remove or add from the phone.
- Stopping a download deletes the local playlist; its songs go at the next cleanup unless they
  are in your top, Liked Songs or another chosen playlist.

Spotify only lets this app read your own (or collaborative) playlists: other people's and
Spotify's automatic ones (Discover Weekly, Daily Mix, editorial...) answer 403/404 and are shown
as not downloadable.

    python spotify_playlists.py                   list your Spotify playlists
    python spotify_playlists.py --download <id>   choose one for download (then ./runner.sh)
    python spotify_playlists.py --stop <id>       stop downloading it
"""

import fcntl
import os
import sys
import threading
from contextlib import contextmanager
from datetime import datetime

from common import SCRIPTS_DIR, DATA_DIR, load_json, save_json

HERE = SCRIPTS_DIR
SUBS_PATH = os.path.join(DATA_DIR, "spotify_playlists.json")
SOURCE_PREFIX = "playlist: "
UPLOAD_MARKER = "[spotify-music]"     # the ones spotify_upload.py uploads: no point downloading them
_thread_lock = threading.Lock()


@contextmanager
def _locked():
    """Lock across threads and processes (the agent chooses; spotify_export.py saves the songs)."""
    with _thread_lock:
        fd = os.open(SUBS_PATH + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def now():
    return datetime.now().isoformat(timespec="seconds")


def load():
    return load_json(SUBS_PATH, {})


def save(subs):
    save_json(SUBS_PATH, subs)


def chosen():
    """The playlists chosen for download: {id: entry}."""
    return {pid: e for pid, e in load().items() if e.get("download")}


# ---------------------------------------------------------------- Spotify
def remote_playlists(sp):
    """Your Spotify playlists (own and followed), with what the phone needs."""
    me = sp.me()["id"]
    subs = load()
    out, page = [], sp.current_user_playlists(limit=50)
    while page:
        for pl in page.get("items") or []:
            if not pl:
                continue
            images = pl.get("images") or []
            sub = subs.get(pl["id"], {})
            out.append({
                "id": pl["id"], "name": pl["name"], "owner": (pl.get("owner") or {}).get("display_name") or "",
                "mine": (pl.get("owner") or {}).get("id") == me,
                "downloadable": (pl.get("owner") or {}).get("id") == me or bool(pl.get("collaborative")),
                "tracks": (pl.get("tracks") or pl.get("items") or {}).get("total", 0),
                "image": images[-1]["url"] if images else None,
                "from_app": UPLOAD_MARKER in (pl.get("description") or ""),
                "download": bool(sub.get("download")), "status": sub.get("status"),
                "local_name": sub.get("local_name"),
            })
        page = sp.next(page) if page.get("next") else None
    return out


def fetch_tracks(sp, playlist_id):
    """Songs of a playlist in order: [{url, title, artist, album, duration}]."""
    rows, offset = [], 0
    while True:
        page = sp.playlist_items(playlist_id, limit=100, offset=offset, additional_types=("track",))
        items = page.get("items") or []
        for it in items:
            t = it.get("track") or it.get("item")
            if not t or t.get("type") != "track" or t.get("is_local") or not (t.get("external_urls") or {}).get("spotify"):
                continue   # episodes, local or removed tracks
            secs = (t.get("duration_ms") or 0) // 1000
            rows.append({"url": t["external_urls"]["spotify"], "title": t["name"],
                         "artist": ", ".join(a["name"] for a in t.get("artists") or []),
                         "album": (t.get("album") or {}).get("name", ""), "duration": f"{secs // 60}:{secs % 60:02d}"})
        offset += len(items)
        if not items or not page.get("next"):
            return rows


def is_unavailable(error):
    """403 (someone else's: Spotify does not allow reading it) or 404 (deleted or a Spotify automatic one)."""
    return getattr(error, "http_status", None) in (403, 404)


# ---------------------------------------------------------------- choose / stop
def set_download(playlist_id, name, download):
    """Marks or unmarks a playlist for download. Returns its entry (or None if removed)."""
    with _locked():
        subs = load()
        entry = subs.get(playlist_id, {"added": now()})
        if download:
            entry.update(download=True, name=name or entry.get("name") or playlist_id, updated=now())
            entry.setdefault("status", "pending")
            subs[playlist_id] = entry
        else:
            subs.pop(playlist_id, None)
            entry = None
        save(subs)
        return entry


def record_tracks(playlist_id, name, tracks, status="ok"):
    with _locked():
        subs = load()
        if playlist_id not in subs:
            return
        subs[playlist_id].update(name=name or subs[playlist_id].get("name"), tracks=tracks, status=status, checked=now())
        save(subs)


def record_status(playlist_id, status):
    with _locked():
        subs = load()
        if playlist_id in subs:
            subs[playlist_id].update(status=status, checked=now())
            save(subs)


def set_local_name(playlist_id, local_name):
    with _locked():
        subs = load()
        if playlist_id in subs:
            subs[playlist_id]["local_name"] = local_name
            save(subs)


def main(args):
    from spotify_auth import SCOPES_PLAYLISTS, SCOPES_READ, get_client
    sp = get_client(f"{SCOPES_READ} {SCOPES_PLAYLISTS}", "spotify_playlists.py")
    if args[:1] == ["--download"] and len(args) > 1:
        pl = sp.playlist(args[1], fields="id,name")
        set_download(pl["id"], pl["name"], True)
        print(f"'{pl['name']}' elegida para descargar; se descargará en el próximo ./runner.sh")
        return 0
    if args[:1] == ["--stop"] and len(args) > 1:
        import playlist_store as ps
        entry = load().get(args[1]) or {}
        set_download(args[1], None, False)
        if entry.get("local_name") and ps.exists(entry["local_name"]):
            ps.delete(entry["local_name"])
        print("Ya no se descarga; sus canciones se quitan en la próxima limpieza si no están en otra lista.")
        return 0
    for p in remote_playlists(sp):
        mark = "↓" if p["download"] else " "
        print(f" {mark} {p['id']}  {p['name']} ({p['tracks']}){'' if p['mine'] else ' · de ' + p['owner']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
