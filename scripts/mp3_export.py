"""
Builds the "MP3 Player Card/" folder for a basic MP3 player (tested with a Yoton YM03, Actions
chip): every MP3 in the library plus one .m3u per playlist, to copy to the root of its microSD card.

Why this way:
  - ASCII-only file names ("Bjork - Joga.mp3"): the firmware reads .m3u paths without UTF-8,
    so it cannot find files with accents, ñ, ç... Titles still show accents (read from the tags).
  - .m3u with relative paths (just the name: MP3s go in the card's root) and CRLF line endings,
    the format the player reads reliably. No .m3u8 (Playlists/ keeps those for the Mac).
  - The MP3s are hard links to "MP3 Songs/": no extra space, and a changed MP3 gets relinked.

    python mp3_export.py                         update "MP3 Player Card/" (runner.sh does it)
    python mp3_export.py --card /Volumes/NAME    also sync the card (and delete M3U.LIB, the
                                                 player's playlist cache): copy new or changed
                                                 files, remove .mp3/.m3u no longer there (and the
                                                 "._*" files macOS creates)
    ... --card /Volumes/NAME --dry-run           only report what it would do on the card
    ... --force                                  go on even if the export has far fewer songs
                                                 than the card (protection against deletions)
"""

import fcntl
import os
import re
import shutil
import sys
import unicodedata
from contextlib import contextmanager

import playlist_store as ps
from common import LOCKS_DIR, ROOT

HERE = ps.HERE
EXPORT_DIR = os.path.join(ROOT, "MP3 Player Card")
MANAGED_EXT = (".mp3", ".m3u")
# The player's playlist cache (Actions chip): while it exists, the player shows the playlists it had
# indexed and does not see new ones. It is deleted on every sync and the player rebuilds it from the
# .m3u files when switched on (without the USB cable).
PLAYER_PLAYLIST_CACHE = "M3U.LIB"
MAX_MISSING = 0.05                 # if more than 5 % of the CSV's MP3s are missing, nothing is touched
MIN_CARD_SHARE = 0.6               # --card does not go on if the export has < 60 % of what is on the card
MAX_NAME = 120                     # characters of the name without extension (FAT32 allows 255)
FAT_FORBIDDEN = '\\/:*?"<>|'
SPECIAL = {"ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "œ": "oe", "Œ": "OE", "ł": "l", "Ł": "L",
           "đ": "d", "Đ": "D", "þ": "th", "Þ": "Th", "’": "'", "‘": "'", "“": "", "”": "", "´": "'",
           "–": "-", "—": "-", "…": "...", "¿": "", "¡": "", "&": "y"}


def ascii_name(text):
    """'Björk - Jóga' -> 'Bjork - Joga'; without characters FAT32 does not allow."""
    s = "".join(SPECIAL.get(c, c) for c in str(text))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode()
    s = "".join("_" if c in FAT_FORBIDDEN or ord(c) < 32 else c for c in s)
    s = re.sub(r"[(\[]\s*(?:feat\.?|ft\.?|with|con)?\s*[)\]]", "", s, flags=re.IGNORECASE)   # "(feat. <non-Latin name>)"
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s[:MAX_NAME].rstrip(" .")


@contextmanager
def export_lock():
    """
    Only one process at a time rebuilds "MP3 Player Card" or copies to the card (runner and mp3_autosync).
    """
    fd = os.open(os.path.join(LOCKS_DIR, ".mp3_export.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class ExportRefused(RuntimeError):
    """The export or the card do not look reliable: nothing is deleted."""

    def __init__(self, message, can_force=False, remove=0):
        super().__init__(message)
        self.can_force, self.remove = can_force, remove   # can_force: the user can confirm and go on


class Mp3Export:
    def __init__(self, export_dir=EXPORT_DIR):
        self.dir = export_dir

    # ------------------------------------------------------------ names
    @staticmethod
    def plan_names(lib):
        """{MP3 path: unique ASCII name (case-insensitive, like FAT32)}."""
        names, used = {}, set()
        for _, row in lib.sort_values("file_path").iterrows():
            path = row["file_path"]
            if path in names or not os.path.exists(path):
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            base = ascii_name(stem)
            title = ascii_name(stem.split(" - ", 1)[-1])
            if not re.search(r"[A-Za-z0-9]", title):
                # Title without Latin letters (e.g. Japanese): told apart by the Spotify id
                artist = ascii_name(stem.split(" - ", 1)[0]) if " - " in stem else ""
                base = f"{artist} - {row['track_id']}" if artist else str(row["track_id"])
            name, n = f"{base}.mp3", 2
            while name.casefold() in used:
                name, n = f"{base} ({n}).mp3", n + 1
            used.add(name.casefold())
            names[path] = name
        return names

    # ------------------------------------------------------------ "MP3 Player Card" folder
    @staticmethod
    def _link(src, dst):
        """Hard link (or a copy if not possible); does nothing if it is already the same file."""
        if os.path.exists(dst):
            if os.path.samefile(src, dst):
                return False
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return True

    def build(self):
        """Updates the folder. Returns a summary {songs, playlists, linked, removed}."""
        with ps.locked:   # nobody rewrites a playlist while exporting
            lib = ps.songs()
            names = self.plan_names(lib)
            paths = set(lib["file_path"].dropna())
            missing = len(paths) - len(names)
            if paths and missing > MAX_MISSING * len(paths):
                # all_songs.csv out of date (MP3s moved or renamed): removing what is "missing" from the folder and
                # the card would delete songs that are still on the Mac
                raise ExportRefused(f"faltan {missing} de {len(paths)} MP3 de all_songs.csv; ejecuta ./runner.sh "
                                    "(o update_csv_schema.py) y vuelve a exportar")
            os.makedirs(self.dir, exist_ok=True)
            wanted, linked = set(), 0
            for src, name in names.items():
                linked += self._link(src, os.path.join(self.dir, name))
                wanted.add(name)
            playlists, taken = 0, {w.casefold() for w in wanted}
            for pl in ps.all_names():
                files = [names[p] for p in ps.read_paths(pl) if p in names]
                base = ascii_name(ps.safe_name(pl)) or "Playlist"
                fname, n = f"{base}.m3u", 2
                while fname.casefold() in taken:   # FAT32 and macOS disks are case-insensitive
                    fname, n = f"{base} ({n}).m3u", n + 1
                self._write_m3u(os.path.join(self.dir, fname), files)
                wanted.add(fname)
                taken.add(fname.casefold())
                playlists += 1
            removed = 0
            for f in os.listdir(self.dir):
                if f.lower().endswith(MANAGED_EXT) and f not in wanted:
                    os.remove(os.path.join(self.dir, f))
                    removed += 1
        return {"songs": len(names), "playlists": playlists, "linked": linked, "removed": removed}

    @staticmethod
    def _write_m3u(path, files):
        content = "#EXTM3U\r\n" + "".join(f"{f}\r\n" for f in files)
        try:
            with open(path, encoding="ascii", newline="") as f:
                if f.read() == content:
                    return
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii", newline="") as f:
            f.write(content)
        os.replace(tmp, path)

    # ------------------------------------------------------------ player card
    @staticmethod
    def _differs(on_card, source):
        """Different size or date (FAT32 stores dates with 2 s precision): needs copying."""
        a, b = os.stat(on_card), os.stat(source)
        # FAT32 stores local time: after a DST change everything looks 1 h off
        diff = abs(a.st_mtime - b.st_mtime)
        return a.st_size != b.st_size or (diff > 2 and abs(diff - 3600) > 2)
    def plan_card(self, card, force=False):
        """
        What the sync would do, touching nothing: {"card", "copy": [...], "remove": [...],
        "on_card": {...}, "source": {...}}. Raises ExportRefused if it is not safe.
        """
        card = os.path.realpath(card)
        if not os.path.isdir(card) or not os.path.ismount(card):
            raise ExportRefused(f"'{card}' no está montada (mira el nombre en el Finder: /Volumes/<NOMBRE>).")
        if not card.startswith("/Volumes/") or os.path.samefile(card, "/"):
            raise ExportRefused(f"'{card}' no parece la tarjeta del reproductor (debe estar en /Volumes/).")
        source = {f: os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.lower().endswith(MANAGED_EXT)}
        on_card = {f.casefold(): f for f in os.listdir(card)}
        copy = [f for f, p in source.items()
                if f.casefold() not in on_card
                or on_card[f.casefold()] != f
                or self._differs(os.path.join(card, on_card[f.casefold()]), p)]
        wanted = {f.casefold() for f in source}
        remove = [f for k, f in on_card.items()
                  if (f.lower().endswith(MANAGED_EXT) and k not in wanted) or f.startswith("._")]
        card_mp3 = sum(f.lower().endswith(".mp3") for f in on_card.values())
        export_mp3 = sum(f.lower().endswith(".mp3") for f in source)
        if card_mp3 and export_mp3 < MIN_CARD_SHARE * card_mp3 and not force:
            raise ExportRefused(f"la exportación tiene {export_mp3} MP3 y la tarjeta {card_mp3}: no se borra nada. "
                                "Si es correcto (has quitado muchas canciones), repite con --force.",
                                can_force=export_mp3 > 0, remove=len(remove))   # never empty the whole card
        cache = on_card.get(PLAYER_PLAYLIST_CACHE.casefold())
        return {"card": card, "copy": copy, "remove": remove, "on_card": on_card, "source": source,
                "player_cache": cache}

    def sync_card(self, card, dry_run=False, force=False, plan=None):
        """Copies new or changed files to the card's root and removes what is left over."""
        plan = plan or self.plan_card(card, force=force)
        card, copy, remove, on_card, source = (plan["card"], plan["copy"], plan["remove"],
                                               plan["on_card"], plan["source"])
        cache_note = f" y su caché de playlists ({plan['player_cache']})" if plan.get("player_cache") else ""
        print(f"Tarjeta {card}: {len(copy)} para copiar, {len(remove)} para quitar{cache_note}.")
        if dry_run:
            for f in sorted(copy):
                print(f"  + {f}")
            for f in sorted(remove):
                print(f"  - {f}")
            if plan.get("player_cache"):
                print(f"  - {plan['player_cache']} (caché de playlists del reproductor; la rehace al encenderse)")
            return
        for f in remove:
            os.remove(os.path.join(card, f))
        for f in sorted(copy, key=lambda x: x.lower().endswith(".m3u")):   # .m3u files last
            existing = on_card.get(f.casefold())
            if existing and existing != f and os.path.exists(os.path.join(card, existing)):
                os.remove(os.path.join(card, existing))   # same name with different case
            dst = os.path.join(card, f)
            shutil.copyfile(source[f], dst)   # without macOS attributes: no "._" files
            st = os.stat(source[f])
            os.utime(dst, (st.st_atime, st.st_mtime))   # same date: so later changes can be detected
        self._drop_player_cache(card)
        print("Tarjeta sincronizada. Expúlsala desde el Finder antes de desconectarla; al encenderlo "
              "(sin cable) el reproductor vuelve a leer las playlists.")

    @staticmethod
    def _drop_player_cache(card):
        """
        Deletes M3U.LIB (at the end, with the .m3u files already copied): otherwise new playlists do not appear.
        """
        for f in os.listdir(card):   # FAT32 is case-insensitive: look it up ignoring case
            if f.casefold() == PLAYER_PLAYLIST_CACHE.casefold() and os.path.isfile(os.path.join(card, f)):
                os.remove(os.path.join(card, f))
                print(f"{f} borrado: el reproductor rehará sus playlists al encenderse.")


def main(argv):
    with export_lock():
        return _main(argv)


def _main(argv):
    export = Mp3Export()
    try:
        summary = export.build()
    except ExportRefused as e:
        print(f"MP3 Player Card: no actualizada: {e}")
        return 1
    print(f"MP3 Player Card: {summary['songs']} MP3 y {summary['playlists']} playlists .m3u "
          f"({summary['linked']} MP3 nuevos o cambiados, {summary['removed']} ficheros quitados).")
    if "--card" in argv:
        i = argv.index("--card")
        if i + 1 >= len(argv):
            print("Falta la ruta de la tarjeta: --card /Volumes/NOMBRE")
            return 1
        try:
            export.sync_card(argv[i + 1], dry_run="--dry-run" in argv, force="--force" in argv)
        except ExportRefused as e:
            print(f"Tarjeta: {e}")
            return 1
        except OSError as e:   # card disconnected or read-only halfway
            print(f"Tarjeta: error al sincronizar: {e}. Vuelve a ejecutarlo con la tarjeta conectada.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
