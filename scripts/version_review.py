"""
Versions to review by hand: songs whose Spotify version is not on YouTube Music, so another one
was downloaded (the studio take of a Spotify live song, a different title or credits, or a user
video because there is no official track). They are downloaded anyway and stay "pending" until
you approve them (kept) or remove them: if it replaced another MP3, the previous one comes back;
otherwise the song leaves the library, the playlists and "MP3 Player Card" and is never
downloaded again.

    python version_review.py               review one by one: [p] play, [a] approve, [q] remove,
                                           [s] skip, [x] exit
    python version_review.py --list        pending, approved and removed
    python version_review.py --approve "Artist - Title"   (or the Spotify id)
    python version_review.py --reject  "Artist - Title"
    python version_review.py --undo    "Artist - Title"   back to pending (a removed one is
                                                          downloaded again by ./runner.sh)

Also from the phone (Home > "Versiones para revisar"), which sends a notification.

data/version_review.json (local to each computer), by Spotify id:
  song, file, spotify_url, video (downloaded YouTube URL), reason, status
  (pending | approved | rejected), added, decided
"""

import json
import os
import subprocess
import sys
import threading

import playlist_store as ps
from common import DATA_DIR, save_json

HERE = ps.HERE
REVIEW_FILE = os.path.join(DATA_DIR, "version_review.json")
PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class ReviewStore:
    """Registry of versions to review. Thread and process safe (uses the playlists lock)."""

    def __init__(self, path=REVIEW_FILE):
        self.path = path
        self._lock = threading.RLock()

    # ------------------------------------------------------------ file
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:
            print(f"Aviso: {os.path.basename(self.path)} está dañado; no se cambia nada")
            raise

    def _save(self, data):
        save_json(self.path, data, sort_keys=True)

    # ------------------------------------------------------------ queries
    def status(self, url):
        return self.load().get(ps.track_id(url), {}).get("status")

    def rejected_ids(self):
        """Removed without a previous MP3: never downloaded again (restored ones already have their MP3)."""
        return {k for k, v in self.load().items() if v.get("status") == REJECTED and not v.get("restored")}

    def pending(self):
        return [{"track_id": k, **v} for k, v in sorted(self.load().items(), key=lambda kv: kv[1].get("added", ""))
                if v.get("status") == PENDING]

    def rejected_songs(self):
        """(title, first artist) of removed songs: the same song under another id is not downloaded either."""
        return [(v.get("title", ""), v.get("artist", "")) for v in self.load().values()
                if v.get("status") == REJECTED and v.get("title") and not v.get("restored")]

    def find(self, text):
        """Id from a Spotify id, "Artist - Title" or file name (case-insensitive)."""
        data = self.load()
        if str(text).strip() in data:   # Spotify ids are case-sensitive
            return str(text).strip()
        t = str(text).strip().casefold().removesuffix(".mp3")
        for k, v in data.items():
            if t in (str(v.get("song", "")).casefold(), os.path.splitext(os.path.basename(v.get("file", "")))[0].casefold()):
                return k
        return None

    # ------------------------------------------------------------ changes
    def add_pending(self, song, file, spotify_url, video, reason, previous=None, title="", artist=""):
        """
        Records a version to review (does not change an approved or removed one). True if new.
        previous: the previous MP3 in the trash if this version replaced another (comes back on removal).
        """
        tid = ps.track_id(spotify_url)
        with self._lock, ps.locked:
            data = self.load()
            if data.get(tid, {}).get("status") in (APPROVED, REJECTED):
                return False
            new = tid not in data
            old = data.get(tid, {})
            data[tid] = {"song": song, "file": file, "spotify_url": spotify_url, "video": video,
                         "reason": reason, "status": PENDING, "added": old.get("added") or ps.now(),
                         "previous": previous or old.get("previous"), "title": title or old.get("title", ""),
                         "artist": artist or old.get("artist", "")}
            self._save(data)
            return new

    def resolve(self, spotify_url):
        """Drops a pending entry that no longer needs review (the MP3 is now Spotify's version)."""
        tid = ps.track_id(spotify_url)
        with self._lock, ps.locked:
            data = self.load()
            if data.get(tid, {}).get("status") == PENDING:
                del data[tid]
                self._save(data)

    def approve(self, tid):
        return self._decide(tid, APPROVED)

    def undo(self, tid):
        entry = self.load().get(tid) or {}
        if entry.get("restored"):
            raise RuntimeError("al quitarla volvió el MP3 anterior; para recuperar la otra versión, "
                               "borra su entrada de version_review.json y ejecuta ./runner.sh")
        return self._decide(tid, PENDING)

    def reject(self, tid):
        """
        You do not like this version:
          - if it replaced another MP3 (version_audit), the previous one comes back and this one goes to
            the trash;
          - otherwise the song leaves: MP3 to the trash, out of all_songs.csv, the playlists and
            "MP3 Player Card", and download_manager never downloads it again.
        Can be repeated (e.g. after a partial failure): the decision is saved at the end.
        """
        from library_cleanup import LibraryCleanup, Trash
        import shutil
        with self._lock, ps.locked:
            entry = self.load().get(tid)
            if entry is None:
                raise KeyError(tid)
            path, previous = entry.get("file"), entry.get("previous")
            if previous and not os.path.exists(previous):
                # Already restored (or the trash was emptied): the current MP3 is the previous one, which is never
                # removed this way
                if entry.get("restored"):
                    return self._decide(tid, REJECTED, restored=True)
                raise RuntimeError("el MP3 anterior ya no está en la papelera (se vacía a los 7 días): apruébala, o "
                                   "bórrala del MP3 a mano y quita su entrada de version_review.json")
            if previous and path:
                if os.path.exists(path):
                    Trash().put(path)
                shutil.move(previous, path)   # the previous one comes back (same name: playlists intact)
                entry = self._decide(tid, REJECTED, restored=True)
            else:
                if path and os.path.exists(path):
                    Trash().put(path)
                LibraryCleanup._prune_songs_csv()
                LibraryCleanup._prune_playlists()
                entry = self._decide(tid, REJECTED, restored=False)
        refresh_export()
        return entry

    def _decide(self, tid, status, **extra):
        with self._lock, ps.locked:
            data = self.load()
            if tid not in data:
                raise KeyError(tid)
            data[tid].update(extra)
            data[tid]["status"] = status
            data[tid]["decided"] = ps.now() if status != PENDING else None
            self._save(data)
            return data[tid]


def refresh_export():
    try:
        from mp3_export import Mp3Export
        Mp3Export().build()
    except Exception as e:
        print(f"Aviso: 'MP3 Player Card' no actualizada: {e}")


def notify_mac(count):
    """Notification on the Mac (the phone's comes from the app when it sees new pending ones)."""
    text = f"{count} versión{'es' if count != 1 else ''} para revisar: python version_review.py, o desde el móvil"
    try:
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "Mi Música"'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------- command line
class _Player:
    """Plays an MP3 on the Mac with afplay (stops when moving to another song)."""

    def __init__(self):
        self.proc = None

    def toggle(self, path):
        if self.proc and self.proc.poll() is None:
            self.stop()
            return
        if os.path.exists(path):
            self.proc = subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


def interactive(store):
    items = store.pending()
    if not items:
        print("No hay versiones pendientes de revisar.")
        return 0
    player = _Player()
    print(f"{len(items)} versiones para revisar. [p] escuchar/parar  [a] aprobar  [q] quitar  [s] saltar  [x] salir\n")
    try:
        for i, it in enumerate(items, 1):
            print(f"{i}/{len(items)}  {it['song']}\n      {it['reason']}\n      {it.get('video', '')}")
            while True:
                choice = input("      > ").strip().lower()
                if choice == "p":
                    player.toggle(it["file"])
                    continue
                player.stop()
                if choice == "a":
                    store.approve(it["track_id"])
                    print("      aprobada\n")
                elif choice == "q":
                    try:
                        done = store.reject(it["track_id"])
                    except RuntimeError as e:
                        print(f"      No se puede: {e}\n")
                        break
                    print("      quitada: " + ("vuelve el MP3 que había antes\n" if done.get("restored") else
                          "MP3 a la papelera, fuera de las playlists; no se volverá a descargar\n"))
                elif choice == "s":
                    print()
                elif choice == "x":
                    return 0
                else:
                    print("      p / a / q / s / x")
                    continue
                break
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        player.stop()
    return 0


def main(argv):
    store = ReviewStore()
    for flag, action in (("--approve", store.approve), ("--reject", store.reject), ("--undo", store.undo)):
        if flag in argv:
            i = argv.index(flag)
            target = argv[i + 1] if i + 1 < len(argv) else ""
            tid = store.find(target)
            if not tid:
                print(f"No encuentro '{target}' en {os.path.basename(REVIEW_FILE)} (mira --list).")
                return 1
            try:
                entry = action(tid)
            except RuntimeError as e:
                print(f"No se puede: {e}")
                return 1
            print(f"{entry['song']}: {entry['status']}" + (" (vuelve el MP3 anterior)" if entry.get("restored") else ""))
            return 0
    if "--list" in argv:
        data = store.load()
        for status in (PENDING, APPROVED, REJECTED):
            rows = [v for v in data.values() if v.get("status") == status]
            print(f"\n{status} ({len(rows)})")
            for v in rows:
                print(f"  {v['song']}  ·  {v['reason']}")
        return 0
    return interactive(store)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
