"""
Library cleanup: removes the MP3s of songs that are no longer in your top tracks (4 weeks,
6 months, all time) or Liked Songs. To keep a recent song, save it to Liked Songs.

Source: a fresh spotify_export.csv. A song is kept if its Spotify id (the MP3's WOAS tag) or its
title + first artist match (same song under another id: single vs. album...).

Songs that are no longer listed:
  - their MP3 goes to "Papelera MP3/<date>/" (recoverable; emptied after 7 days)
  - they are removed from all_songs.csv and from every playlist (custom ones too)
  - their failed downloads are forgotten (descargas_fallidas.json)

Safety: does nothing if the export is older than 12 h, lacks Liked Songs or top tracks, or would
remove more than 40 % of the library at once (e.g. Spotify returned an incomplete list).
--force skips these checks.

  python library_cleanup.py --dry-run    what would be removed, nothing is touched
  python library_cleanup.py              clean up (runner.sh runs it after the export)
  python library_cleanup.py --force

Exit codes: 0 ok, 3 skipped for safety (runner.sh carries on).
"""

import csv
import glob
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime

from mutagen.id3 import ID3

import playlist_store
from common import SCRIPTS_DIR, ROOT, DATA_DIR, load_json, norm, save_json

HERE = SCRIPTS_DIR
MUSIC_DIR = os.path.join(ROOT, "MP3 Songs")
EXPORT_CSV = os.path.join(DATA_DIR, "spotify_export.csv")
SONGS_CSV = os.path.join(DATA_DIR, "all_songs.csv")
FAILED_FILE = os.path.join(DATA_DIR, "descargas_fallidas.json")
TRASH_DIR = os.path.join(ROOT, "Papelera MP3")
TRASH_DAYS = 7
MAX_EXPORT_AGE_HOURS = 12
MAX_REMOVE_FRACTION = 0.40
EXIT_SKIPPED = 3


FEAT = re.compile(r"\s[\(\[-]\s?(?:ft|feat|with|con)\b|\s\((?:ft|feat|with|con)\b", re.I)


def song_key(title, artists):
    """(title without "(ft. ...)", first artist): the same song even if the id or the ft. changes."""
    t = FEAT.split(str(title))[0]
    return norm(t) or "raw:" + t.strip().lower(), norm(str(artists).split(",")[0].split("/")[0])


class SafetyCheckFailed(Exception):
    pass


# ---------------------------------------------------------------- what to keep
class KeepList:
    """Songs of spotify_export.csv: Spotify ids and title + first artist keys."""

    def __init__(self, path=EXPORT_CSV):
        self.path = path
        with open(path, encoding="utf-8-sig") as f:
            self.rows = [r for r in csv.DictReader(f) if r.get("spotify_url")]
        self.ids = {playlist_store.track_id(r["spotify_url"]) for r in self.rows}
        self.keys = {song_key(r["title"], r["artist"]) for r in self.rows}
        self.urls = {r["spotify_url"] for r in self.rows}

    def keeps(self, track):
        return track.track_id in self.ids or track.key in self.keys

    def check(self):
        """Raises SafetyCheckFailed if the export does not look complete and recent."""
        age_h = (time.time() - os.path.getmtime(self.path)) / 3600
        if age_h > MAX_EXPORT_AGE_HOURS:
            raise SafetyCheckFailed(f"spotify_export.csv tiene {age_h:.0f} h; ejecuta antes spotify_export.py")
        labels = {lab.strip() for r in self.rows for lab in r.get("source", "").split(" / ")}
        if "liked" not in labels or not any(lab.startswith("top - ") for lab in labels):
            raise SafetyCheckFailed("spotify_export.csv no trae favoritas y top; ¿es de una versión anterior?")


# ---------------------------------------------------------------- what is on disk
@dataclass
class LocalTrack:
    path: str
    track_id: str   # "" if the MP3 has no Spotify link
    key: tuple
    label: str


class LocalLibrary:
    def __init__(self, music_dir=MUSIC_DIR):
        self.tracks = [self._read(p) for p in sorted(glob.glob(os.path.join(music_dir, "**", "*.mp3"), recursive=True))]

    @staticmethod
    def _read(path):
        try:
            tags = ID3(path)
        except Exception:
            tags = {}
        url = next((f.url for k, f in tags.items() if k.startswith("WOAS") and "open.spotify.com/track/" in f.url), "")
        title, artist = str(tags.get("TIT2", "")), str(tags.get("TPE1", ""))
        return LocalTrack(path, playlist_store.track_id(url.split("?")[0]), song_key(title, artist),
                          f"{artist} - {title}" if title else os.path.basename(path))


# ---------------------------------------------------------------- trash
class Trash:
    """Papelera MP3/<date>/: recoverable; folders older than TRASH_DAYS days are deleted."""

    def __init__(self, root=TRASH_DIR, days=TRASH_DAYS):
        self.root, self.days = root, days

    def put(self, path):
        folder = os.path.join(self.root, date.today().isoformat())
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, os.path.basename(path))
        if os.path.exists(target):
            base, ext = os.path.splitext(target)
            target = f"{base} ({datetime.now():%H%M%S}){ext}"
        shutil.move(path, target)
        return target

    @staticmethod
    def _kept_for_review(folder):
        """MP3s in this folder that a pending review would restore if rejected (version_review.py)."""
        try:
            from version_review import PENDING, ReviewStore
            refs = {os.path.realpath(v["previous"]) for v in ReviewStore().load().values()
                    if v.get("status") == PENDING and v.get("previous")}
        except Exception:
            return set()
        return {r for r in refs if r.startswith(os.path.realpath(folder) + os.sep)}

    def purge(self):
        if not os.path.isdir(self.root):
            return 0
        purged = 0
        for name in os.listdir(self.root):
            try:
                day = date.fromisoformat(name)
            except ValueError:
                continue
            if (date.today() - day).days > self.days:
                folder = os.path.join(self.root, name)
                keep = self._kept_for_review(folder)
                if keep:   # previous MP3s of versions still under review: delete only the rest
                    for f in os.listdir(folder):
                        p = os.path.join(folder, f)
                        if os.path.realpath(p) not in keep:
                            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                    continue
                shutil.rmtree(folder, ignore_errors=True)
                purged += 1
        return purged


# ---------------------------------------------------------------- cleanup
class LibraryCleanup:
    def __init__(self, keep, library, trash, force=False):
        self.keep, self.library, self.trash, self.force = keep, library, trash, force

    def plan(self):
        """(to remove, kept without a Spotify link)."""
        remove, unknown = [], []
        for t in self.library.tracks:
            if self.keep.keeps(t):
                continue
            (remove if t.track_id else unknown).append(t)
        return remove, unknown

    def check(self, remove):
        if self.force:
            return
        self.keep.check()
        total = len(self.library.tracks)
        if total and len(remove) > MAX_REMOVE_FRACTION * total:
            raise SafetyCheckFailed(f"se quitarían {len(remove)} de {total} MP3 (más del "
                                    f"{MAX_REMOVE_FRACTION:.0%}); revísalo con --dry-run y usa --force si es correcto")

    def apply(self, remove):
        for t in remove:
            self.trash.put(t.path)
        self._prune_songs_csv()
        touched = self._prune_playlists()
        self._prune_failed()
        return touched

    @staticmethod
    def _prune_songs_csv():
        if not os.path.exists(SONGS_CSV):
            return
        with open(SONGS_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            fields, rows = reader.fieldnames, [r for r in reader if os.path.exists(r["file_path"])]
        tmp = SONGS_CSV + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, SONGS_CSV)

    @staticmethod
    def _prune_playlists():
        """Removes from every playlist the MP3s that no longer exist. Returns {name: songs removed}."""
        touched = {}
        lib = playlist_store.songs()
        for name in playlist_store.all_names():
            before = len(playlist_store.read_paths(name))
            after = playlist_store.drop_missing_files(name, lib)
            if after != before:
                touched[name] = before - after
        return touched

    def _prune_failed(self):
        failed = load_json(FAILED_FILE, {}, warn=print)
        kept = {url: v for url, v in failed.items() if url in self.keep.urls}
        if len(kept) != len(failed):
            save_json(FAILED_FILE, kept)


def main(args):
    dry_run, force = "--dry-run" in args, "--force" in args
    if not os.path.exists(EXPORT_CSV):
        # Nothing exported yet (fresh install): nothing to compare against, so nothing is removed
        print("Limpieza omitida: aún no hay spotify_export.csv; ejecuta antes spotify_export.py")
        return 3
    cleanup = LibraryCleanup(KeepList(), LocalLibrary(), Trash(), force=force)
    remove, unknown = cleanup.plan()
    total = len(cleanup.library.tracks)
    print(f"Limpieza: {total} MP3; {len(cleanup.keep.rows)} canciones en tu top y favoritas; "
          f"{len(remove)} ya no están en ninguna lista.")
    if unknown:
        print(f"Se conservan {len(unknown)} MP3 sin enlace de Spotify (no se puede comprobar): "
              + ", ".join(t.label for t in unknown[:5]) + ("..." if len(unknown) > 5 else ""))
    for t in remove[:200]:
        print(f"  - {t.label}")
    if len(remove) > 200:
        print(f"  ... y {len(remove) - 200} más")

    try:
        cleanup.check(remove)
    except SafetyCheckFailed as e:
        print(f"Limpieza omitida: {e}")
        return EXIT_SKIPPED
    if dry_run:
        print("[dry-run] No se ha tocado nada.")
        return 0

    purged = cleanup.trash.purge()
    if purged:
        print(f"Papelera: vaciadas {purged} carpetas de más de {TRASH_DAYS} días.")
    if not remove:
        print("Limpieza: no hay nada que quitar.")
        return 0
    touched = cleanup.apply(remove)
    print(f"Limpieza: {len(remove)} MP3 movidos a '{os.path.basename(TRASH_DIR)}' (se borran a los {TRASH_DAYS} días).")
    for name, n in touched.items():
        print(f"  playlist '{name}': -{n}")
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main(sys.argv[1:]))
