"""
Downloads as MP3 (spotdl) the songs of spotify_export.csv that are not yet in MP3 Songs/.

Exit codes: 0 ok (even if some song is not found: it is kept in descargas_fallidas.json), 1 if
spotify_export.csv or spotdl is missing, or if there were 2+ new songs and none could be
downloaded (no network, YouTube blocked...). A single song missing from YouTube is not a failure.
"""

import concurrent.futures
import csv
import glob
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from mutagen.id3 import ID3

from common import SCRIPTS_DIR, ROOT, DATA_DIR, load_json, norm, save_json, track_id
from version_review import ReviewStore, notify_mac
from youtube_source import YouTubeSource, is_live_title, title_key, to_seconds, unwanted_in, video_id_from_url

# Settings
HERE = SCRIPTS_DIR   # absolute paths: works from any folder
CSV_FILE = os.path.join(DATA_DIR, "spotify_export.csv")
OUTPUT_DIR = os.path.join(ROOT, "MP3 Songs")
FAILED_FILE = os.path.join(DATA_DIR, "descargas_fallidas.json")  # {spotify_url: {"song": ..., "runs": n}}
MAX_WORKERS = 4
MAX_RETRIES = 3
MAX_FAILED_RUNS = 3      # give up after 3 failed runs (delete its entry to retry)
SPOTDL_TIMEOUT = 300     # seconds per attempt, so a stuck download does not block the run
SOURCE = YouTubeSource()
REVIEW = ReviewStore()      # versions different from Spotify's, to review by hand
_new_reviews = []
# spotdl from the same environment as this Python (independent of PATH, e.g. in unattended runs)
SPOTDL = os.path.join(os.path.dirname(sys.executable), "spotdl")
if not os.path.exists(SPOTDL):
    SPOTDL = shutil.which("spotdl") or "spotdl"

def ensure_directory():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

def load_songs():
    if not os.path.exists(CSV_FILE):
        print(f"Error: {CSV_FILE} no encontrado.")
        return []
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if row.get("spotify_url")]

def scan_local_files():
    """
    What is really on disk: Spotify URLs (WOAS tag written by spotdl) and (title, first artist)
    pairs to detect the same song under another id.
    """
    urls, keys = set(), set()
    for path in glob.glob(os.path.join(OUTPUT_DIR, "**", "*.mp3"), recursive=True):
        try:
            tags = ID3(path)
        except Exception:
            continue
        for key, frame in tags.items():
            if key.startswith("WOAS"):
                urls.add(frame.url.split("?")[0])
        artist = str(tags.get("TPE1", "")).split("/")[0]
        keys.add((title_key(tags.get("TIT2", "")), norm(artist)))
    return urls, keys

def load_failed():
    return load_json(FAILED_FILE, {}, warn=print)

def save_failed(failed):
    save_json(FAILED_FILE, failed)

def search_youtube_music(song):
    """
    Official studio track on YouTube Music (see youtube_source.py): by title, artist and duration,
    without live takes or versions (karaoke, piano, cover...). spotdl's matching often discards
    correct results and, when it succeeds, may pick a music video or a live take; so it is given the
    video already chosen.
    Returns find_studio's result (with 'same'=False if it is another version, to review) or None.
    """
    try:
        return SOURCE.find_studio(song["title"], [a.strip() for a in song["artist"].split(",")],
                                  to_seconds(song.get("duration")), allow_different=True)
    except Exception as e:
        print(f"Aviso: búsqueda en YouTube Music fallida para {song['artist']} - {song['title']}: {e}")
        return None

def downloaded_file(song, folder=OUTPUT_DIR):
    """The song's MP3 in `folder` (by its WOAS tag), or None."""
    url = song["spotify_url"]
    for path in glob.glob(os.path.join(folder, "*.mp3")):
        try:
            tags = ID3(path)
        except Exception:
            continue
        if any(k.startswith("WOAS") and f.url.split("?")[0] == url for k, f in tags.items()):
            return path
    return None

def check_fallback(song):
    """
    After a download chosen by spotdl (there was no official track):
      ("reject", text)   not the original song (karaoke, piano, cover...): deleted;
      ("review", reason) a user video or a live take Spotify does not have: kept, pending manual
                         review;
      None               official music video or no video data: accepted.
    """
    path = downloaded_file(song)
    if not path:
        return None
    artists = [a.strip() for a in song["artist"].split(",")]
    try:
        comm = next((str(f) for k, f in ID3(path).items() if k.startswith("COMM")), "")
        video = video_id_from_url(comm)
        info = SOURCE.video_info(video) if video else None
    except Exception:
        return None   # without video data it is not discarded
    if not info:
        return None
    if unwanted_in(info, song["title"], artists):
        os.remove(path)
        return "reject", f"{info['title']} / {info['author']}"
    problems = [p for p in SOURCE.problems(info, song["title"], artists) if p != "videoclip"]
    if problems and not (problems == ["directo"] and is_live_title(song["title"])):
        return "review", f"No hay pista oficial; descargada de '{info['title']}' / {info['author']} ({', '.join(problems)})"
    return None

def mark_for_review(song, video_url, reason):
    path = downloaded_file(song)
    if path and REVIEW.add_pending(f"{song['artist']} - {song['title']}", path, song["spotify_url"], video_url, reason,
                                   title=song["title"], artist=song["artist"].split(",")[0].strip()):
        _new_reviews.append(song["spotify_url"])
        print(f"🔎 Para revisar: {song['artist']} - {song['title']}: {reason}")

_live = set()          # running spotdl processes (to stop them all if interrupted)
_live_lock = threading.Lock()
_stopping = threading.Event()   # interrupted: no new downloads or retries are started

def stop_all_spotdl():
    _stopping.set()
    with _live_lock:
        for p in list(_live):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass

def run_spotdl(query, extra_args=(), output=OUTPUT_DIR):
    if _stopping.is_set():
        return False, "interrumpido"
    command = [SPOTDL, "download", query, "--output", output, *extra_args]
    # Own process group: if it hangs, its ffmpeg is killed too (otherwise a half-written MP3 would remain)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    with _live_lock:
        _live.add(proc)
        if _stopping.is_set():   # a stop was requested right while it was starting
            os.killpg(proc.pid, signal.SIGKILL)
    try:
        output, _ = proc.communicate(timeout=SPOTDL_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return False, "timeout"
    finally:
        with _live_lock:
            _live.discard(proc)
    # spotdl returns 0 even if it does not find the song, so the output is checked. "file already
    # exists" = the MP3 is already there (under another id or with unreadable tags): also fine
    ok = proc.returncode == 0 and ("Downloaded" in output or "file already exists" in output)
    return ok, output

def download_track(song):
    url = song["spotify_url"]
    label = f"{song['artist']} - {song['title']}"
    print(f"Iniciando descarga: {label}")
    for attempt in range(MAX_RETRIES):
        if _stopping.is_set():
            return False
        try:
            # 1st our own YouTube Music search, 2nd spotdl's (YouTube Music, then YouTube)
            if attempt <= 1 and (found := search_youtube_music(song)):
                ok, output = run_spotdl(f"{found['url']}|{url}")
                if ok and not found["same"]:
                    mark_for_review(song, found["url"], found["reason"])
            else:
                ok, output = run_spotdl(url, ["--audio", "youtube-music", "youtube"])
                verdict = check_fallback(song) if ok else None
                if verdict and verdict[0] == "reject":
                    print(f"❌ Descartada (no es la original: {verdict[1]}): {label}")
                    return False
                if verdict:
                    try:
                        comm = next((str(f) for k, f in ID3(downloaded_file(song)).items() if k.startswith("COMM")), "")
                    except Exception:
                        comm = ""
                    mark_for_review(song, comm, verdict[1])
            if ok:
                print(f"✅ Éxito: {label}")
                return True
            if _stopping.is_set():
                return False
            print(f"⚠️ Reintentando {label} ({attempt + 1}/{MAX_RETRIES})...")
            time.sleep(2)
        except Exception as e:
            print(f"❌ Error con {label}: {e}")
            break
    print(f"❌ Fallo definitivo: {label}")
    return False

def main():
    ensure_directory()
    if not os.path.exists(CSV_FILE):
        print(f"Error: {CSV_FILE} no encontrado.")
        return 1
    if not os.path.exists(SPOTDL) and not shutil.which(SPOTDL):
        print(f"Error: no se encuentra spotdl ({SPOTDL}).")
        return 1
    songs = load_songs()
    # Versions you removed while reviewing them are never downloaded again (nor the same song under another id)
    rejected = REVIEW.rejected_ids()
    rejected_keys = {(title_key(t), norm(a)) for t, a in REVIEW.rejected_songs()}
    songs = [s for s in songs if track_id(s["spotify_url"]) not in rejected
             and (title_key(s["title"]), norm(s["artist"].split(",")[0])) not in rejected_keys]
    local_urls, local_keys = scan_local_files()
    failed = load_failed()

    new_songs, skipped = [], []
    for song in songs:
        if song["spotify_url"] in local_urls:
            continue
        # Same song downloaded under another Spotify id (single vs. album, etc.)
        if (title_key(song["title"]), norm(song["artist"].split(",")[0])) in local_keys:
            continue
        if failed.get(song["spotify_url"], {}).get("runs", 0) >= MAX_FAILED_RUNS:
            skipped.append(song)
            continue
        new_songs.append(song)

    print(f"Encontradas {len(songs)} canciones en {CSV_FILE}; {len(local_urls)} MP3 en disco.")
    if skipped:
        print(f"Se omiten {len(skipped)} que fallaron en {MAX_FAILED_RUNS} ejecuciones (ver {FAILED_FILE}).")
    print(f"Se van a descargar {len(new_songs)} canciones nuevas.")

    if new_songs:
        # Ctrl-C (SIGINT) or the runner/agent stopped (SIGTERM): also stop the running spotdl processes,
        # which have their own process group and do not receive the signal
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            results = list(executor.map(download_track, new_songs))
        except BaseException as e:
            executor.shutdown(wait=False, cancel_futures=True)
            stop_all_spotdl()
            print("Descargas interrumpidas; se paran las que estaban en marcha.")
            if isinstance(e, KeyboardInterrupt):
                return 130   # Ctrl-C: clean exit, no traceback
            raise
        executor.shutdown()

        for song, ok in zip(new_songs, results):
            if ok:
                failed.pop(song["spotify_url"], None)
            else:
                entry = failed.setdefault(song["spotify_url"], {"song": f"{song['artist']} - {song['title']}", "runs": 0})
                entry["runs"] += 1
        n_failed = sum(not ok for ok in results)
        print(f"Descargadas {len(new_songs) - n_failed}/{len(new_songs)}." + (f" Fallidas en {os.path.basename(FAILED_FILE)}." if n_failed else ""))
    else:
        n_failed = 0
        print("No hay canciones nuevas para descargar. ¡Estás al día!")

    save_failed(failed)
    if _new_reviews:
        print(f"Para revisar: {len(_new_reviews)} canciones con otra versión (python version_review.py o desde el móvil).")
        notify_mac(len(REVIEW.pending()))
    if len(new_songs) >= 2 and n_failed == len(new_songs):
        print("Error: no se ha podido descargar ninguna canción (¿sin red o YouTube bloqueado?).")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
