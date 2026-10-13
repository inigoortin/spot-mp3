"""
Checks which YouTube video each MP3 came from and, if it is not the official studio track (a live
take, a music video, a cover, a piano or karaoke version, a user lyric video...), replaces it with
the YouTube Music studio track when there is one. If the best match is not Spotify's version (the
studio take of a live song, a different title) or there is only a user video, it is left pending
manual review (version_review.py, or from the phone).

    python version_audit.py            check and replace (runner.sh does it after the downloads)
    python version_audit.py --dry-run  report only, touch nothing
    python version_audit.py --recheck  check everything again (ignores version_audit.json)

The previous MP3 goes to "Papelera MP3/<date>/" (recoverable for 7 days); if the new download
fails, nothing changes. The new MP3 keeps the old file name (all_songs.csv and playlists stay
valid). version_audit.json remembers what was checked; songs without a studio track are searched
again after RETRY_DAYS days, failed downloads on the next run.

Exit codes: 0 ok (even if some could not be replaced), 1 unexpected error.
"""

import concurrent.futures
import csv
import glob
import os
import fcntl
import shutil
import signal
import sys
import tempfile
import threading
from datetime import date, datetime

from mutagen.id3 import ID3
from mutagen.mp3 import MP3

import download_manager as dm
from common import LOCKS_DIR, DATA_DIR, load_json, save_json
import playlist_store as ps
from version_review import APPROVED, REJECTED, ReviewStore, notify_mac
from library_cleanup import Trash
from youtube_source import YouTubeSource, is_live_title, to_seconds, video_id_from_url

HERE = dm.HERE
CACHE_FILE = os.path.join(DATA_DIR, "version_audit.json")
RETRY_DAYS = 14
WORKERS = 6


class LocalSong:
    """A library MP3 with what is needed to check it."""

    def __init__(self, path, tags, export):
        self.path = path
        self.name = os.path.basename(path)
        url = next((f.url.split("?")[0] for k, f in tags.items() if k.startswith("WOAS")), "")
        comm = next((str(f) for k, f in tags.items() if k.startswith("COMM")), "")
        self.spotify_url = url
        self.video_id = video_id_from_url(comm)
        row = export.get(url, {})
        self.title = row.get("title") or str(tags.get("TIT2", ""))
        artists = row.get("artist") or str(tags.get("TPE1", "")).replace("/", ",")
        self.artists = [a.strip() for a in artists.split(",") if a.strip()]
        self.duration = to_seconds(row.get("duration"))
        if self.duration is None:   # no longer in spotify_export.csv: the MP3's own link
            try:
                self.duration = round(MP3(path).info.length)
            except Exception:
                self.duration = None

    @property
    def song(self):
        """spotify_export.csv format used by download_manager."""
        return {"title": self.title, "artist": ", ".join(self.artists), "spotify_url": self.spotify_url}


class VersionAudit:
    def __init__(self, dry_run=False, recheck=False, source=None, trash=None):
        self.dry_run, self.recheck = dry_run, recheck
        self.source = source or YouTubeSource()
        self.trash = trash or Trash()
        self.cache = {} if recheck else self._load_cache()
        self.review, self.new_reviews = ReviewStore(), 0
        self._trash_lock = threading.Lock()

    # ------------------------------------------------------------ cache
    @staticmethod
    def _load_cache():
        return load_json(CACHE_FILE, {})

    def _save_cache(self):
        save_json(CACHE_FILE, self.cache)

    def _fresh(self, song):
        """True if this file was already checked with this same video and is not due again."""
        c = self.cache.get(song.name)
        if not c or c.get("video") != song.video_id:
            return False
        if c.get("status") == "ok":
            return True
        if c.get("status") == "failed":
            return False
        try:
            return (date.today() - date.fromisoformat(c.get("checked", ""))).days < RETRY_DAYS
        except ValueError:
            return False

    # ------------------------------------------------------------ check
    @staticmethod
    def library():
        export = {}
        if os.path.exists(dm.CSV_FILE):
            with open(dm.CSV_FILE, encoding="utf-8-sig") as f:
                export = {r["spotify_url"]: r for r in csv.DictReader(f) if r.get("spotify_url")}
        out = []
        for path in sorted(glob.glob(os.path.join(dm.OUTPUT_DIR, "*.mp3"))):
            try:
                out.append(LocalSong(path, ID3(path), export))
            except Exception:
                continue
        return out

    def inspect(self, song):
        """(problems of the current video, proposed studio track or None)."""
        if dm._stopping.is_set():
            raise RuntimeError("interrumpido")
        if not song.video_id:
            return ["sin vídeo de origen"], None, None
        info = self.source.video_info(song.video_id)
        problems = self.source.problems(info, song.title, song.artists)
        if not problems:
            return [], None, None
        found = self.source.find_studio(song.title, song.artists, song.duration, allow_different=True)
        if found and found["video_id"] == song.video_id:
            found = None   # the best available is the one we have
        if found and found.get("similar") and not self.keep_reason(problems, song, info):
            # What we have is already Spotify's version (an official music video with the same title): do not
            # replace it with a similar title
            found = None
        labelled = [f"{p}: {info['title']} / {info['author']}" if i == 0 else p for i, p in enumerate(problems)]
        return labelled, found, (None if found else self.keep_reason(problems, song, info))

    @staticmethod
    def keep_reason(problems, song, info):
        """
        If there is nothing better and the MP3 is not official (or is a live take Spotify does not have): to review.
        """
        rest = [p for p in problems if p != "videoclip"]
        if not rest or (rest == ["directo"] and is_live_title(song.title)):
            return None   # official music video, or the same live take as Spotify
        return f"No hay pista oficial; el MP3 viene de '{info['title']}' / {info['author']} ({', '.join(rest)})"

    def replace(self, song, found):
        """
        Downloads the studio track into a temporary folder and, only if it arrives fine (an MP3 with the
        song's WOAS tag), moves the old one to the trash and leaves the new one under the SAME file name
        (so all_songs.csv, playlists and "MP3 Player Card" still point to it). If anything fails, the
        library is untouched. Returns the path of the old MP3 in the trash if replaced, or None.
        """
        # Outside the library (and without a leading dot: spotdl strips it from the folder name and would
        # download into "MP3 Songs/version_x", where the other scripts would see it)
        tmp = tempfile.mkdtemp(prefix="mimusica_version_")
        old = None
        try:
            ok, output = dm.run_spotdl(f"{found['url']}|{song.spotify_url}", output=tmp)
            new = dm.downloaded_file(song.song, folder=tmp)
            if not (ok and new):
                last = output.strip().splitlines()[-1] if output.strip() else "sin salida"
                print(f"   no se pudo descargar {song.name} ({last}); se mantiene la anterior")
                return None
            with self._trash_lock:
                old = self.trash.put(song.path)
                shutil.move(new, song.path)
            return old
        except Exception as e:
            print(f"   error al cambiar {song.name}: {e}; se mantiene la anterior")
            if old and not os.path.exists(song.path):
                shutil.move(old, song.path)
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _review(self, song, video_url, reason, previous=None):
        if self.review.add_pending(f"{', '.join(song.artists)} - {song.title}", song.path, song.spotify_url,
                                   video_url, reason, previous=previous, title=song.title,
                                   artist=song.artists[0] if song.artists else ""):
            self.new_reviews += 1
            print(f"   🔎 para revisar: {reason}")

    def run(self):
        # Songs you already reviewed (approved or removed) are not touched
        decided = {k for k, v in self.review.load().items() if v.get("status") in (APPROVED, REJECTED)}
        songs = [s for s in self.library() if s.spotify_url and ps.track_id(s.spotify_url) not in decided]
        todo = [s for s in songs if not self._fresh(s)]
        print(f"Versiones: {len(songs)} MP3; se revisan {len(todo)} (el resto ya revisadas).")
        today = date.today().isoformat()
        to_replace, kept, errors = [], 0, 0
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            futures = {ex.submit(self.inspect, s): s for s in todo}
            for fut in concurrent.futures.as_completed(futures):
                s = futures[fut]
                try:
                    problems, found, keep_reason = fut.result()
                except Exception as e:
                    errors += 1
                    print(f"Aviso: no se pudo revisar {s.name}: {e}")
                    continue
                if not problems:
                    self.cache[s.name] = {"video": s.video_id, "status": "ok", "checked": today}
                elif not found:
                    kept += 1
                    print(f"·  {s.name}: {'; '.join(problems)} (sin pista de estudio; se mantiene)")
                    if keep_reason and not self.dry_run:
                        self._review(s, f"https://www.youtube.com/watch?v={s.video_id}", keep_reason)
                    self.cache[s.name] = {"video": s.video_id, "status": "no_studio", "checked": today}
                else:
                    print(f"→  {s.name}: {'; '.join(problems)}  ⇒  {found['title']} ({found['url']})")
                    to_replace.append((s, found))
        if self.dry_run:
            print(f"Versiones: se cambiarían {len(to_replace)} por la pista de estudio; {kept} sin alternativa; "
                  f"{errors} sin poder revisar (prueba: no se ha tocado nada).")
            return 0

        changed = 0
        # Two MP3s pointing to the same video are not downloaded at once (spotdl uses the same temp file)
        rounds, seen = [[]], {}
        for item in to_replace:
            n = seen.get(item[1]["video_id"], -1) + 1
            seen[item[1]["video_id"]] = n
            while len(rounds) <= n:
                rounds.append([])
            rounds[n].append(item)
        for batch in rounds:
          with concurrent.futures.ThreadPoolExecutor(dm.MAX_WORKERS) as ex:
            for (s, found), new in zip(batch, ex.map(lambda x: self.replace(*x), batch)):
                if new:
                    changed += 1
                    if not found["same"]:
                        # "Remove" while reviewing restores the previous MP3 (the song is never left without an MP3)
                        self._review(s, found["url"], found["reason"], previous=new)
                    else:
                        self.review.resolve(s.spotify_url)   # already Spotify's version: nothing to review
                    self.cache[s.name] = {"video": found["video_id"], "status": "ok", "checked": today,
                                          "replaced": s.video_id, "when": datetime.now().isoformat(timespec="seconds")}
                else:   # failed download: retried on the next run
                    self.cache[s.name] = {"video": s.video_id, "status": "failed", "checked": today}
                self._save_cache()   # saved as we go: if interrupted, finished work is not repeated
        self._save_cache()
        if self.new_reviews:
            notify_mac(len(self.review.pending()))
            print(f"Versiones: {self.new_reviews} nuevas para revisar (python version_review.py o desde el móvil).")
        print(f"Versiones: {changed}/{len(to_replace)} cambiadas por la pista de estudio (las anteriores en "
              f"'Papelera MP3'); {kept} sin alternativa; {errors} sin poder revisar.")
        return 0


LOCK_FILE = os.path.join(LOCKS_DIR, ".version_audit.lock")


def main(argv):
    # One check at a time (runner.sh and by hand)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Versiones: ya hay otra revisión en marcha; se omite.")
        return 0
    # Stopping (runner/agent or Ctrl-C) also stops the spotdl processes, which run in their own process
    # group. The library is never left half done: each MP3 is only replaced once the new one is downloaded
    def stop(signum, _frame):
        # stop_all_spotdl flags the stop: pending checks and running downloads end right away and nothing
        # else is changed
        dm.stop_all_spotdl()
        sys.exit(143 if signum == signal.SIGTERM else 130)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return VersionAudit(dry_run="--dry-run" in argv, recheck="--recheck" in argv).run()
    finally:
        dm.stop_all_spotdl()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
