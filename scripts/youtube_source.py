"""
Quality of each MP3's YouTube source: it should be the original studio recording.

YouTube Music classifies every video (musicVideoType):
  ATV  official album audio track ("Art Track"): the studio recording. Preferred.
  OMV  official music video: often a live take, a session or the video edit (intro, noise,
       another mix).
  UGC  and untyped videos: uploaded by anyone (lyric versions, covers, piano, karaoke...).

So to download or review a song, its official track (ATV) is searched by title, artist and
duration, skipping live takes and non-original versions (karaoke, instrumental, piano, cover...)
unless the Spotify title itself asks for them. If the Spotify song is a live take, its studio
version is searched too: studio audio sounds better and the video is not watched here.
"""

import re
import threading
import time
import unicodedata

from common import norm

MAX_DURATION_DIFF = 15        # maximum difference in seconds from the Spotify duration
LIVE_DURATION_DIFF = 90       # if Spotify has the live take, the studio one can be quite different in length

LIVE_WORDS = ["live", "en vivo", "en directo", "directo", "ao vivo", "en viu", "en concierto", "concierto",
              "concert", "unplugged", "tiny desk", "live session"]
UNWANTED_WORDS = ["karaoke", "instrumental", "piano", "cover", "covers", "drumcover", "pianocover",
                  "guitarcover", "version propia", "8d", "slowed", "sped up", "speed up", "reverb", "nightcore",
                  "acapella", "a capella", "tutorial", "bass boosted", "remix", "mashup"]
# Variants of the same song that are not valid replacements for the original (unless Spotify asks for them)
VARIANT_WORDS = ["acoustic", "acustico", "acustica", "acustic", "version", "versio", "edit", "mix", "demo",
                 "orchestral", "sinfonico", "sped", "slowed", "extended", "intro", "interlude"]


def norm_text(s):
    """Lowercase, without accents, only letters/digits separated by one space."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " " + " ".join(re.findall(r"[a-z0-9]+", s)) + " "


norm_key = norm   # like norm_text but without spaces (for comparing titles and artists)


def words_in(text, words):
    t = norm_text(text)
    return {w for w in words if f" {w} " in t}


def extra_words(candidate, spotify_title, words, names=()):
    """
    Words from the list that the candidate has and the Spotify title does not. `names` (artists,
    channel) are removed first: "The Piano Guys" does not make "Cello Song" a piano version.
    """
    t = norm_text(candidate)
    for n in names:
        n = norm_text(n).strip()
        if n:
            t = t.replace(f" {n} ", " ")
    return words_in(t, words) - words_in(spotify_title, words)


_FEAT = re.compile(r"\s*(?:[(\[]\s*(?:feat\.?|ft\.?|featuring|with|con|w/)\s[^)\]]*[)\]]"
                   r"|[-–—]\s*(?:feat\.?|ft\.?|featuring|with|con)\s[^()\[\]]*$)", re.IGNORECASE)
_SAME_RECORDING = re.compile(r"\s*(?:[-–—]\s*|[(\[])\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|remasterizad[oa]|radio edit|"
                             r"single version|album version|original mix)\b[^()\[\]]*[)\]]?\s*$", re.IGNORECASE)


def core_title(title):
    """Title without "(feat. X)" or "- Remastered 2009": what identifies the recording."""
    t = str(title)
    for _ in range(3):
        t2 = _SAME_RECORDING.sub("", _FEAT.sub("", t)).strip()
        if t2 == t:
            break
        t = t2
    return t


def drop_artist_credits(title, artists):
    """'Song (Artist A & Artist B)' -> 'Song' if the parenthesis holds only artists."""
    known = {norm_key(a) for a in artists if norm_key(a)}

    def only_artists(m):
        parts = [norm_key(p) for p in re.split(r"\s*(?:&|,|\by\b|\band\b|\bx\b)\s*", m.group(1), flags=re.IGNORECASE)]
        parts = [p for p in parts if p]
        return "" if parts and all(p in known for p in parts) else m.group(0)

    return re.sub(r"\s*[(\[]([^()\[\]]+)[)\]]", only_artists, str(title)).strip()


def title_key(title):
    """Key for comparing titles: Latin letters without accents; if there are none, the title as is."""
    core = core_title(title)
    plain = "".join(c for c in unicodedata.normalize("NFKD", core.casefold()) if not unicodedata.combining(c))
    if not any(c.isalpha() and not c.isascii() for c in plain):
        return norm_key(plain)   # Latin letters only (with or without accents)
    # Other alphabets, or mixed: every letter counts, not only Latin ones
    return "raw:" + "".join(c for c in plain if c.isalnum())


_LIVE_SUFFIX = re.compile(
    r"\s*(?:[-–—]\s*|\(|\[)[^-–—()\[\]]*\b(?:live|en vivo|en directo|directo|ao vivo|en viu|concierto|concert|"
    r"unplugged|tiny desk)\b[^()\[\]]*[)\]]?\s*$", re.IGNORECASE)


def is_live_title(title):
    """Live take according to its Spotify title: "X - En Vivo", "X (Live at...)" ("Live Forever" is not)."""
    return studio_title(title) != str(title).strip()


def studio_title(title):
    """'Song - En Vivo' -> 'Song'; '(Live at X)' removed. Unchanged if not live."""
    t = str(title).strip()
    for _ in range(2):
        t2 = _LIVE_SUFFIX.sub("", t).strip()
        if not t2 or t2 == t:
            break
        t = t2
    return t


def to_seconds(duration):
    """'3:04' -> 184 (None if it cannot be parsed)."""
    try:
        m, s = str(duration).split(":")
        return int(m) * 60 + int(s)
    except ValueError:
        return None


class YouTubeSource:
    """Finds a song's studio track and reports what an already downloaded video is."""

    RETRIES = 3

    def __init__(self):
        self._local = threading.local()   # YTMusic is not thread-safe: one per thread

    @property
    def yt(self):
        if not hasattr(self._local, "yt"):
            from ytmusicapi import YTMusic
            self._local.yt = YTMusic()
        return self._local.yt

    def _call(self, fn, *args, **kwargs):
        """Retries network / rate-limit errors; if it keeps failing, raises the error."""
        for attempt in range(self.RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception:
                if attempt == self.RETRIES - 1:
                    raise
                time.sleep(2 * (attempt + 1))

    # ------------------------------------------------------------ already downloaded video
    def video_info(self, video_id):
        """{'type': 'ATV'|'OMV'|'UGC'|'', 'title', 'author', 'length'} of the video."""
        vd = self._call(self.yt.get_song, video_id).get("videoDetails") or {}
        return {"type": str(vd.get("musicVideoType", "")).replace("MUSIC_VIDEO_TYPE_", ""),
                "title": vd.get("title", ""), "author": vd.get("author", ""),
                "length": int(vd.get("lengthSeconds") or 0)}

    @staticmethod
    def problems(info, song_title, artists=()):
        """Why a video is not the original studio recording ([] if it is)."""
        text = f"{info.get('title', '')} {info.get('album', '')} {info.get('author', '')}"
        out = []
        unwanted = unwanted_in(info, song_title, artists)
        if unwanted:
            out.append("no es la versión original (" + ", ".join(sorted(unwanted)) + ")")
        if extra_words(text, song_title, LIVE_WORDS, artists) or is_live_title(song_title):
            out.append("directo")
        if info.get("type") != "ATV":
            out.append({"OMV": "videoclip", "UGC": "subido por un usuario"}.get(info.get("type"), "vídeo sin clasificar"))
        return out

    # ------------------------------------------------------------ search
    def find_studio(self, title, artists, duration=None, allow_different=False):
        """
        Best official studio track (ATV) on YouTube Music for the song, or None.
        title: Spotify title; artists: list of artists; duration: Spotify seconds.
        Returns {'url', 'video_id', 'title', 'live', 'same', 'reason'}:
          same=True   it is the Spotify version (same title without "feat."/"Remastered", artist and
                      duration ±15 s; if Spotify has a live take, that same live take);
          same=False  another version, to review by hand (version_review.py): the studio version of a
                      Spotify live take or, with allow_different, a similar but not identical title
                      ("Song" for "Song Con Artist"). `reason` explains it.
        """
        live_song = is_live_title(title)
        base = studio_title(title) if live_song else title
        key = title_key(drop_artist_credits(base, artists))
        artist_keys = [norm_key(a) for a in artists if norm_key(a)]
        results = self._call(self.yt.search, f"{artists[0] if artists else ''} {base}", filter="songs", limit=10)

        exact, studio_of_live, live, similar = [], [], [], []
        for r in results or []:
            if not r.get("videoId") or not r.get("title"):
                continue
            if r.get("videoType") not in (None, "MUSIC_VIDEO_TYPE_ATV"):
                continue
            r_title = r["title"]
            names = list(artists) + [a.get("name", "") for a in r.get("artists") or []]
            # "X (En Vivo)" is compared as "X" (and counts as live); "(A & B)" artist lists removed
            r_key = title_key(drop_artist_credits(studio_title(r_title), names))
            r_artists = [norm_key(a.get("name")) for a in r.get("artists") or []]
            album = (r.get("album") or {}).get("name", "")
            if (not any(a in r_artists for a in artist_keys)
                    or extra_words(f"{r_title} {album}", base, UNWANTED_WORDS, names)
                    or extra_words(r_title, base, VARIANT_WORDS, names)):
                continue
            dur = abs((r.get("duration_seconds") or 0) - duration) if duration else None
            rank = (0 if norm_key(r_title) == norm_key(base) else 1, dur or 0)
            is_live = bool(extra_words(f"{r_title} {album}", base, LIVE_WORDS, names))
            if r_key == key:
                if is_live:
                    # A live take only qualifies if Spotify's is live too, with the same duration
                    if live_song and dur is not None and dur <= MAX_DURATION_DIFF:
                        live.append((rank, r))
                elif live_song:
                    if dur is not None and dur <= LIVE_DURATION_DIFF:
                        studio_of_live.append((rank, r))
                elif dur is None or dur <= MAX_DURATION_DIFF:
                    exact.append((rank, r))
            elif (allow_different and not is_live and dur is not None and dur <= MAX_DURATION_DIFF
                  and min(len(key), len(r_key)) >= 3 and (key in r_key or r_key in key)):
                similar.append((rank, r))

        def pick(group, is_live, same, reason):
            r = min(group, key=lambda x: x[0])[1]
            return {"url": f"https://music.youtube.com/watch?v={r['videoId']}", "video_id": r["videoId"],
                    "title": r["title"], "live": is_live, "same": same, "reason": reason.format(t=r["title"]),
                    "similar": group is similar}

        if exact:
            return pick(exact, False, True, "")
        if studio_of_live:   # better sound, but not the Spotify recording: to review
            return pick(studio_of_live, False, False, "Spotify tiene el directo; descargada la versión de estudio ('{t}')")
        if live:
            return pick(live, True, True, "")
        if similar:
            return pick(similar, False, False, "No está la versión de Spotify; descargada '{t}' (título distinto)")
        return None


def unwanted_in(info, song_title, artists=()):
    """
    Non-original version words (karaoke, piano, cover...) in the video title. The channel name only
    counts if it is not the artist's ("Sound Piano Music" yes, "The Piano Guys" no).
    """
    title_words = extra_words(info.get("title", ""), song_title, UNWANTED_WORDS, artists)
    author = info.get("author", "")
    channel = norm_text(re.sub(r"(?i)\s*-\s*topic$|vevo$", "", author)).strip()
    author_is_artist = any(norm_text(a).strip() and norm_text(a).strip() == channel for a in artists)
    author_words = set() if author_is_artist else extra_words(author, song_title, UNWANTED_WORDS, artists)
    return title_words | author_words


def video_id_from_url(url):
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", str(url or ""))
    return m.group(1) if m else None
