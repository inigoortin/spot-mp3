"""
Fills all_songs.csv with:
  - spotify_url: from the WOAS ID3 tag spotdl writes into each MP3 (fallback: spotify_export.csv,
    matched by title + artist).
  - energy, danceability, valence, acousticness, speechiness (0-100), genre, genre_family, key,
    mode and camelot: from the local Essentia analysis (essentia_features.json), so every track
    is measured with the same algorithm.
  - energy_group, danceability_group, valence_group: thirds of your own library
    (Low = bottom 33 %, High = top 33 %).
  - sp_energy, sp_danceability, ...: Spotify catalogue values via ReccoBeats, for reference only
    (missing for some songs; do not filter on them).

Genre (per artist, Spotify style: "Spanish Pop", "Spanish Indie", "Urbano Latino"...):
  1. data/artist_genres.csv (artist;genre): your editable table, always wins.
  2. Otherwise it is guessed from the MusicBrainz country, the language of the titles and the
     Essentia sound family (genre_source = "auto"; worth reviewing and moving to the table).
  genre_family groups genres (Pop, Indie, Urban, Rap, Rock...); genre_style keeps the Discogs
  style Essentia hears in each song, for reference. `genre` follows genre_auto unless you edit
  it by hand in the CSV.

  python enrich_spotify_metrics.py   (after essentia_analyzer.py)
"""

import os
import sys
import time

import pandas as pd
import requests
from mutagen.id3 import ID3

from common import CACHE_DIR, SCRIPTS_DIR, DATA_DIR, load_json, save_json, track_id

HERE = SCRIPTS_DIR   # absolute paths: works from any folder
CSV_PATH = os.path.join(DATA_DIR, "all_songs.csv")
EXPORT_BACKUPS = [os.path.join(DATA_DIR, "spotify_export.csv")]
ESSENTIA_PATH = os.path.join(DATA_DIR, "essentia_features.json")
ARTIST_GENRES_PATH = os.path.join(DATA_DIR, "artist_genres.csv")
MUSICBRAINZ_CACHE_PATH = os.path.join(CACHE_DIR, "musicbrainz_cache.json")
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/artist/"
MUSICBRAINZ_UA = "SpotifyPlaylistMaker/1.0 (personal use)"
CATALOG_CACHE_PATH = os.path.join(CACHE_DIR, "reccobeats_cache.json")
CATALOG_URL = "https://api.reccobeats.com/v1/audio-features"
BATCH_SIZE = 40

# instrumentalness is dropped: the model does not tell voice/instrumental apart well on this kind of library
METRICS = ["energy", "danceability", "valence", "acousticness", "speechiness"]
# Styles tied to specific markets that the model assigns by "sound" and that rarely fit; the next most
# likely style is used instead (only affects genre_style)
GENRE_BLOCKLIST = {
    "Pop---K-pop", "Pop---J-pop", "Pop---City Pop", "Pop---Kayōkyoku", "Pop---Bollywood",
    "Pop---Schlager", "Folk, World, & Country---Volksmusik", "Electronic---Disco Polo",
    "Folk, World, & Country---Laïkó", "Folk, World, & Country---Hindustani",
    "Folk, World, & Country---Indian Classical",
}
GENRE_FAMILY = {
    "Spanish Pop": "Pop", "Catalan Pop": "Pop", "Latin Pop": "Pop", "Pop": "Pop",
    "Italian Pop": "Pop", "K-pop": "Pop", "J-pop": "Pop", "Indian Pop": "Pop",
    "Spanish Indie": "Indie", "Indie Pop": "Indie",
    "Spanish Urban": "Urban", "Urbano Latino": "Urban",
    "Spanish Rap": "Rap", "Hip Hop": "Rap",
    "Spanish Rock": "Rock", "Rock": "Rock",
    "Electronic": "Electronic", "Country": "Country", "Classical": "Classical",
    "Regional Mexicano": "Regional", "Brazilian": "Regional",
}
SPAIN = {"ES", "AD"}
LATAM = {"MX", "CO", "AR", "PR", "CL", "PE", "VE", "EC", "DO", "CU", "PA", "UY", "PY", "BO",
         "CR", "GT", "HN", "SV", "NI"}
SPANISH_WORDS = {"el", "la", "los", "las", "de", "que", "te", "tu", "mi", "me", "yo", "no", "y",
                 "en", "por", "para", "con", "amor", "una", "un", "sin", "del", "lo", "se", "es"}
PORTUGUESE_WORDS = {"e", "o", "da", "do", "não", "vem", "você", "meu", "minha", "eu", "ó", "deus",
                    "fala", "aqui", "brota", "zuar", "beber", "paredao", "fogo", "gasolina", "já"}
ENGLISH_WORDS = {"the", "you", "i", "my", "me", "love", "of", "and", "in", "it", "to", "a",
                 "your", "is", "on", "we", "for", "all", "what", "don't", "i'm"}
GROUP_LABELS = {
    "energy": ["Low", "Medium", "High"],
    "danceability": ["Low", "Medium", "High"],
    "valence": ["Dark/Sad", "Neutral", "Happy/Upbeat"],
}


def norm(s):
    return str(s).strip().lower()


def url_from_tags(file_path):
    try:
        tags = ID3(file_path)
    except Exception:
        return ""
    for key, frame in tags.items():
        if key.startswith("WOAS") and "open.spotify.com/track/" in frame.url:
            return frame.url.split("?")[0]
    return ""


def load_cache(path):
    """Cache or previous results: if missing or corrupt, start empty (it gets refilled)."""
    return load_json(path, {}, warn=lambda msg: print("  " + msg))


def fetch_catalog(ids, cache):
    """Spotify catalogue values (via ReccoBeats) for the ids not in the cache."""
    pending = [i for i in ids if i and i not in cache]
    try:
        for start in range(0, len(pending), BATCH_SIZE):
            batch = pending[start:start + BATCH_SIZE]
            resp = requests.get(CATALOG_URL, params={"ids": ",".join(batch)}, timeout=30)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 5)))
                resp = requests.get(CATALOG_URL, params={"ids": ",".join(batch)}, timeout=30)
            resp.raise_for_status()
            for item in resp.json().get("content", []):
                cache[track_id(item["href"])] = item
            # Ids without data are marked so they are not requested again
            for i in batch:
                cache.setdefault(i, None)
            print(f"  Catálogo ReccoBeats: {min(start + BATCH_SIZE, len(pending))}/{len(pending)}")
            time.sleep(0.5)
    finally:
        save_json(CATALOG_CACHE_PATH, cache)   # what was downloaded is saved even if it fails halfway
    return cache


def load_artist_genres():
    """artist (normalised) -> genre"""
    if not os.path.exists(ARTIST_GENRES_PATH):
        return {}
    table = pd.read_csv(ARTIST_GENRES_PATH, sep=";", encoding="utf-8-sig").fillna("")
    return {norm(a): g for a, g in zip(table["artist"], table["genre"]) if g}


def musicbrainz_country(artist, cache):
    """Artist's country in MusicBrainz (max 1 request/s); None if there is no clear match."""
    if artist in cache:
        return cache[artist]
    country = None
    try:
        resp = requests.get(
            MUSICBRAINZ_URL, params={"query": f'artist:"{artist}"', "fmt": "json", "limit": 3},
            headers={"User-Agent": MUSICBRAINZ_UA}, timeout=20,
        )
        resp.raise_for_status()   # 503 rate limit, etc.: retried another day, not cached
        for a in resp.json().get("artists", []):
            if norm(a.get("name")) == norm(artist) and a.get("score", 0) >= 90:
                country = a.get("country") or (a.get("area") or {}).get("iso-3166-1-codes", [None])[0]
                break
    except Exception:
        return None  # not cached: retried on the next run
    cache[artist] = country
    time.sleep(1.1)
    return country


def title_language(texts):
    """'es', 'pt', 'en' or None from the frequent words in titles and albums."""
    words = [w.strip(",.!?-") for t in texts for w in str(t).lower().replace("(", " ").replace(")", " ").split()]
    pt = sum(w in PORTUGUESE_WORDS or any(c in w for c in "ãõç") for w in words)
    es = sum(w in SPANISH_WORDS or any(c in w for c in "ñ¿¡") for w in words)
    es += sum(any(c in w for c in "áéíóú") for w in words if w not in PORTUGUESE_WORDS)
    en = sum(w in ENGLISH_WORDS for w in words)
    scores = {"es": es, "pt": pt, "en": en}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 and list(scores.values()).count(scores[best]) == 1 else None


def auto_genre(country, language, family):
    """Default genre for artists not in artist_genres.csv."""
    urban = family in ("Latin", "Hip Hop", "Reggae")
    if country == "BR" or language == "pt":
        return "Brazilian"
    # MusicBrainz sometimes matches a namesake from another country: if the titles are in Spanish and
    # the country is not Spanish-speaking, trust the titles
    if country in SPAIN or (language == "es" and country not in LATAM):
        if family == "Rock":
            return "Spanish Indie"  # the model hears "rock" in Spanish acoustic pop
        return "Spanish Urban" if urban else "Spanish Pop"
    if country in LATAM:
        return "Urbano Latino" if urban else "Latin Pop"
    if family == "Latin":
        return "Urbano Latino"
    if family == "Hip Hop":
        return "Hip Hop"
    if family == "Rock":
        return "Rock"
    return "Pop"


def pick_style(features):
    """Most likely (family, style) outside the blocklist."""
    if not features:
        return None
    for label, _ in features["genre_top"]:
        if label not in GENRE_BLOCKLIST:
            return label.split("---")
    return features["genre_top"][0][0].split("---")


def bpm_range(bpm):
    """Usual tempo ranges."""
    if pd.isna(bpm) or bpm <= 0:
        return "Unknown"
    for upper, label in [(90, "60-90"), (110, "90-110"), (125, "110-125"), (140, "125-140")]:
        if bpm < upper:
            return label
    return "140+"


def tertile_groups(values, labels):
    """Assigns Low/Medium/High from the library's thirds; empty -> Unknown."""
    valid = values.dropna()
    if valid.empty:
        return pd.Series("Unknown", index=values.index)
    low, high = valid.quantile([1 / 3, 2 / 3])
    groups = pd.Series(labels[1], index=values.index)
    groups[values <= low] = labels[0]
    groups[values > high] = labels[2]
    groups[values.isna()] = "Unknown"
    return groups


def main():
    df = pd.read_csv(CSV_PATH, sep=";", dtype={"spotify_url": str}, encoding="utf-8-sig")
    print(f"Cargadas {len(df)} pistas de {CSV_PATH}")

    # 1. Recover spotify_url
    df["spotify_url"] = df["spotify_url"].fillna("")
    missing = df["spotify_url"] == ""
    df.loc[missing, "spotify_url"] = df.loc[missing, "file_path"].map(url_from_tags)

    missing = df["spotify_url"] == ""
    backup_path = next((p for p in EXPORT_BACKUPS if os.path.exists(p)), None)
    if missing.any() and backup_path:
        backup = pd.read_csv(backup_path) if backup_path.endswith(".csv") else pd.read_excel(backup_path)
        # artist_name is only the first artist: the key uses the export's first artist too
        lookup = {(norm(t), norm(str(a).split(",")[0])): u for t, a, u in zip(backup["title"], backup["artist"], backup["spotify_url"])}
        df.loc[missing, "spotify_url"] = [
            lookup.get((norm(t), norm(a)), "") for t, a in zip(df.loc[missing, "track_name"], df.loc[missing, "artist_name"])
        ]
    print(f"spotify_url recuperada en {(df['spotify_url'] != '').sum()}/{len(df)} pistas")

    # 2. Essentia metrics (0-100 scale)
    essentia = load_cache(ESSENTIA_PATH)
    features = [essentia.get(p) for p in df["file_path"]]
    if sum(f is not None for f in features) < 0.5 * len(df):
        # Essentia cache missing or broken: the CSV's metrics are not erased
        print(f"Error: solo {sum(f is not None for f in features)}/{len(df)} pistas tienen análisis de Essentia; no se guarda el CSV.")
        sys.exit(1)
    for metric in METRICS:
        df[metric] = pd.array([round(f[metric] * 100) if f else pd.NA for f in features], dtype="Int64")
    for metric, labels in GROUP_LABELS.items():
        df[f"{metric}_group"] = tertile_groups(df[metric], labels)

    # BPM (Essentia RhythmExtractor2013)
    df["bpm_exact"] = [f.get("bpm") if f else pd.NA for f in features]
    df["bpm_range"] = df["bpm_exact"].map(bpm_range)

    # 3. Genre per artist, key and mode
    styles = [pick_style(f) for f in features]
    df["genre_style"] = [s[1] if s else "" for s in styles]

    # Main artist according to Spotify
    export = pd.read_csv(backup_path) if backup_path and backup_path.endswith(".csv") else None
    full_artists = dict(zip(export["spotify_url"], export["artist"])) if export is not None else {}
    albums = dict(zip(export["spotify_url"], export["album"])) if export is not None else {}
    main_artist = [str(full_artists.get(u, a)).split(",")[0].strip() for u, a in zip(df["spotify_url"], df["artist_name"])]

    table = load_artist_genres()
    mb_cache = load_cache(MUSICBRAINZ_CACHE_PATH)
    by_artist = {}
    for artist in dict.fromkeys(main_artist):
        if norm(artist) in table:
            by_artist[artist] = (table[norm(artist)], "artist_table")
            continue
        rows = [i for i, a in enumerate(main_artist) if a == artist]
        families = pd.Series([styles[i][0] for i in rows if styles[i]])
        family = families.mode().iloc[0] if not families.empty else ""
        texts = [df["track_name"].iloc[i] for i in rows] + [albums.get(df["spotify_url"].iloc[i], "") for i in rows]
        country = musicbrainz_country(artist, mb_cache)
        by_artist[artist] = (auto_genre(country, title_language(texts), family), "auto")
    save_json(MUSICBRAINZ_CACHE_PATH, mb_cache)

    old_auto = df["genre_auto"].fillna("") if "genre_auto" in df else pd.Series("", index=df.index)
    df["genre_auto"] = [by_artist[a][0] for a in main_artist]
    df["genre_source"] = [by_artist[a][1] for a in main_artist]
    # Only overwrite the genre if it was not edited by hand
    current = df["genre"].fillna("")
    replace = current.isin(["", "Unassigned"]) | (current == old_auto)
    df.loc[replace, "genre"] = [a or "Unassigned" for a in df.loc[replace, "genre_auto"]]
    df["genre_family"] = df["genre"].map(GENRE_FAMILY).fillna("Other")
    unmapped = sorted(set(df.loc[~df["genre"].isin(list(GENRE_FAMILY)) & (df["genre"] != "Unassigned"), "genre"].dropna()))
    if unmapped:
        print(f"  Aviso: géneros sin familia (quedan como 'Other'; ¿errata en {ARTIST_GENRES_PATH}?): {', '.join(unmapped)}")
    auto_artists = sorted({a for a, (_, src) in by_artist.items() if src == "auto"})
    print(f"Género: {len(by_artist) - len(auto_artists)} artistas de {ARTIST_GENRES_PATH}, {len(auto_artists)} automáticos")
    df["key"] = [f["key"] if f else "" for f in features]
    df["mode"] = [f["mode"] if f else "" for f in features]
    df["camelot"] = [f["camelot"] if f else "" for f in features]

    analyzed = sum(f is not None for f in features)
    print(f"Essentia: {analyzed}/{len(df)} pistas analizadas")
    if analyzed < len(df):
        print("  Sin analizar (ejecuta .venv-essentia/bin/python essentia_analyzer.py):")
        for p, f in zip(df["file_path"], features):
            if f is None:
                print(f"  - {os.path.basename(p)}")

    # 4. Spotify catalogue values, for reference only
    ids = df["spotify_url"].map(track_id)
    try:
        catalog = fetch_catalog(sorted(set(ids) - {""}), load_cache(CATALOG_CACHE_PATH))
    except requests.RequestException as e:
        # Reference values only: without network, use whatever is cached
        print(f"  Aviso: no se pudo consultar ReccoBeats ({e}); se usa la caché")
        catalog = load_cache(CATALOG_CACHE_PATH)
    for metric in METRICS:
        df[f"sp_{metric}"] = pd.array(
            [round(catalog[i][metric] * 100) if catalog.get(i) else pd.NA for i in ids], dtype="Int64"
        )
    print(f"Catálogo de Spotify (referencia): {df['sp_energy'].notna().sum()}/{len(df)} pistas")

    df = df.drop(columns=["metrics_source", "instrumentalness", "sp_instrumentalness", "language"], errors="ignore")
    tmp = CSV_PATH + ".tmp"
    df.to_csv(tmp, index=False, sep=";", encoding="utf-8-sig")  # BOM: Excel shows accents and ñ correctly
    os.replace(tmp, CSV_PATH)  # never leaves the CSV half written
    print(f"Guardado {CSV_PATH}")


if __name__ == "__main__":
    main()
