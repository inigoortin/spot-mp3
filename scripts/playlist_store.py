"""
Playlist registry and editing, shared by playlist_builder.py, library_cleanup.py, spotify_upload.py
and the phone agent (agent_server.py).

Each playlist is a Playlists/<name>.m3u8 file (MP3 paths) and Playlists/playlists.json keeps, by name:
  source      "preset" | "custom" | "spotify"
  criteria, limit, order_by     to rebuild it
  excluded    ids removed by hand (from the phone): they never come back, even if they match
  pinned      ids added (or restored) by hand: they stay even if they no longer match
  was_pinned  pinned ids that were later removed: undoing the removal pins them again
  deleted     True if an example playlist was deleted: runner.sh does not recreate it
  spotify_url link of the playlist in Spotify, if published
  created, updated

Playlists are rebuilt on every runner.sh run (new matching songs come in), always respecting
excluded and pinned. Songs are identified by their Spotify id (open.spotify.com/track/<id>).

Concurrency: the agent and the scripts are separate processes. Every read-modify-write of the
registry or of a .m3u8 happens inside `with locked:` (thread and process lock, Playlists/.lock).

Names: the file is safe_name(name), so "Rock & Roll" and "Rock _ Roll" share a file (and macOS
disks are case-insensitive). Each file belongs to a single registry name (resolve()); creating a
playlist whose name exists or clashes with another is refused (name_problem()). A loose .m3u8
whose name is not a valid own file name (e.g. "A&B.m3u8" copied by hand) is not shown.
"""

import fcntl

import json
import os
import threading
from datetime import datetime

import pandas as pd

import common
from common import SCRIPTS_DIR, ROOT, DATA_DIR

HERE = SCRIPTS_DIR
PLAYLISTS_DIR = os.path.join(ROOT, "Playlists")
TRASH_DIR = os.path.join(PLAYLISTS_DIR, ".papelera")
REGISTRY = os.path.join(PLAYLISTS_DIR, "playlists.json")
SONGS_CSV = os.path.join(DATA_DIR, "all_songs.csv")
FORBIDDEN_CHARS = "/%\\"   # they break the agent's and the app's paths


class _StoreLock:
    """Reentrant lock across threads (RLock) and processes (flock on Playlists/.lock)."""

    def __init__(self):
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._rlock.acquire()
        try:
            if self._depth == 0:
                os.makedirs(PLAYLISTS_DIR, exist_ok=True)
                self._fd = os.open(os.path.join(PLAYLISTS_DIR, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._rlock.release()


locked = _StoreLock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def safe_name(name):
    return "".join(c if c.isalnum() or c in " -_+()" else "_" for c in name).strip()


track_id = common.track_id   # other modules use it as playlist_store.track_id


# ---------------------------------------------------------------- registry
def load():
    if os.path.exists(REGISTRY):
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(registry):
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    common.save_json(REGISTRY, registry)


def entry(name):
    return load().get(resolve(name), {})


def register(name, **fields):
    """Creates or updates a playlist's entry (only changes the fields passed)."""
    with locked:
        registry = load()
        current = registry.get(name, {"created": now()})
        current.update(fields)
        current["updated"] = now()
        registry[name] = current
        save(registry)
        return current


def path_for(name):
    return os.path.join(PLAYLISTS_DIR, f"{safe_name(name)}.m3u8")


def file_key(name):
    """Identifies a playlist's file (macOS disks are case-insensitive)."""
    return safe_name(name).casefold()


def resolve(name, registry=None):
    """
    Registry name that `name` belongs to: itself if registered; otherwise the registered playlist
    that uses the same file; if none, `name` itself.
    """
    registry = load() if registry is None else registry
    if name in registry:
        return name
    target = file_key(name)
    return next((n for n, e in registry.items() if not e.get("deleted") and file_key(n) == target), name)


def name_problem(name):
    """Why a playlist cannot be created with that name (or None if it can)."""
    import playlist_builder  # imported here to avoid a circular import
    name = str(name).strip()
    if not name or not safe_name(name).strip("_ "):
        return "el nombre está vacío"
    if any(c in name for c in FORBIDDEN_CHARS):
        return "el nombre no puede llevar / % ni \\"
    key = file_key(name)
    preset = next((p for p in playlist_builder.PRESETS if file_key(p) == key), None)
    if preset:
        return (f"'{preset}' es una de las playlists de ejemplo; elige otro nombre (si la borraste, se "
                f"recupera en el Mac con: python3 playlist_builder.py \"{preset}\")")
    other = next((n for n in load() if file_key(n) == key), None)
    if other or os.path.exists(path_for(name)):
        return f"ya existe una playlist con ese nombre o muy parecido ('{other or resolve(name)}')"
    return None


def exists(name):
    return not entry(name).get("deleted") and os.path.exists(path_for(name))


def is_edited(e):
    return bool(e.get("excluded") or e.get("pinned"))


def all_names():
    """
    Registered playlists (except deleted ones) and loose .m3u8 files in Playlists/ that belong to no
    registered playlist (one file = one playlist).
    """
    registry = load()
    names = [n for n, e in registry.items() if not e.get("deleted") and os.path.exists(path_for(n))]
    claimed = {file_key(n) for n in registry}
    if os.path.isdir(PLAYLISTS_DIR):
        for f in sorted(os.listdir(PLAYLISTS_DIR)):
            base, ext = os.path.splitext(f)
            path = os.path.join(PLAYLISTS_DIR, f)
            if ext == ".m3u8" and file_key(base) not in claimed and path_for(base) == path:
                names.append(base)
                claimed.add(file_key(base))
    return names


# ---------------------------------------------------------------- library
def songs():
    df = pd.read_csv(SONGS_CSV, sep=";", encoding="utf-8-sig")
    df["track_id"] = df["spotify_url"].map(track_id)
    return df


# ---------------------------------------------------------------- songs of a playlist
def read_paths(name):
    path = path_for(name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def read_tracks(name, lib=None):
    """List of dicts {track_id, artist, title, file_path, spotify_url} in order."""
    lib = (songs() if lib is None else lib).drop_duplicates("file_path").set_index("file_path")
    out = []
    for p in read_paths(name):
        if p in lib.index:
            row = lib.loc[p]
            out.append({"track_id": row["track_id"], "artist": row["artist_name"], "title": row["track_name"],
                        "file_path": p, "spotify_url": row["spotify_url"]})
    return out


def write_local(name, rows):
    """rows: list of dicts with artist, title, file_path (player's M3U8 format)."""
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
    path = path_for(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"#PLAYLIST:{name}\n")
        for r in rows:
            f.write(f"#EXTINF:-1,{r['artist']} - {r['title']}\n{r['file_path']}\n")
    os.replace(tmp, path)
    return path


def edit(name, add=(), remove=(), restore=()):
    """
    Removes/adds songs (Spotify ids) and remembers it for future rebuilds:
      - remove: goes to `excluded` (never comes back) and is unpinned if it was pinned;
      - add (or restore one from "Quitadas a mano"): leaves `excluded` and is pinned in `pinned`,
        so it stays even if it stops matching the criteria;
      - restore ("Undo" right after removing): back to how it was before: leaves `excluded` and is
        pinned again only if it was pinned.
    Only library songs can be added (they need the MP3). Repeating the same edit changes nothing
    (the phone may resend it).
    """
    with locked:
        name = resolve(name)
        if not exists(name):
            raise KeyError(name)
        lib = songs()
        by_id = lib.drop_duplicates("track_id").set_index("track_id")
        e = entry(name)
        excluded, pinned = list(e.get("excluded") or []), list(e.get("pinned") or [])
        was_pinned = list(e.get("was_pinned") or [])
        remove, restore = set(remove), list(restore)

        kept = [t for t in read_tracks(name, lib) if t["track_id"] not in remove]
        present = {t["track_id"] for t in kept}
        rejected = []
        for tid in list(add) + restore:
            if tid not in by_id.index:
                rejected.append(tid)
                continue
            if tid not in present:
                row = by_id.loc[tid]
                kept.append({"track_id": tid, "artist": row["artist_name"], "title": row["track_name"],
                             "file_path": row["file_path"]})
                present.add(tid)
            if tid in excluded:
                excluded.remove(tid)
            if (tid not in restore or tid in was_pinned) and tid not in pinned:
                pinned.append(tid)
            if tid in was_pinned:
                was_pinned.remove(tid)
        for tid in remove:
            if tid in pinned:
                pinned.remove(tid)
                if tid not in was_pinned:
                    was_pinned.append(tid)
            if tid not in excluded:
                excluded.append(tid)

        write_local(name, kept)
        register(name, excluded=excluded, pinned=pinned, was_pinned=was_pinned)
        return {"count": len(kept), "rejected": rejected}

def drop_missing_files(name, lib=None):
    """Removes from the playlist the paths whose MP3 no longer exists. Returns how many remain."""
    with locked:
        tracks = [t for t in read_tracks(name, lib) if os.path.exists(t["file_path"])]
        if len(tracks) != len(read_paths(name)):
            write_local(name, tracks)
        return len(tracks)


def delete(name):
    """
    Moves the file to Playlists/.papelera (recoverable). An example playlist is marked as deleted so
    runner.sh does not recreate it.
    """
    import playlist_builder  # imported here to avoid a circular import
    with locked:
        name = resolve(name)
        path = path_for(name)
        if os.path.exists(path):
            os.makedirs(TRASH_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(path, os.path.join(TRASH_DIR, f"{stamp} {os.path.basename(path)}"))
        registry = load()
        if name in playlist_builder.PRESETS:
            registry[name] = {**registry.get(name, {}), "deleted": True, "updated": now()}
        else:
            registry.pop(name, None)
        save(registry)
