"""
Builds data/all_songs.csv: one row per MP3 in "MP3 Songs/" with title, first artist and Spotify
link from the ID3 tags, keeping the metric columns that enrich_spotify_metrics.py fills in.
"""

import os
import sys
import pandas as pd
import glob
from mutagen.id3 import ID3, ID3NoHeaderError
from common import SCRIPTS_DIR, ROOT, DATA_DIR

MUSIC_DIR = os.path.join(ROOT, "MP3 Songs")
CSV_PATH = os.path.join(DATA_DIR, "all_songs.csv")

HERE = SCRIPTS_DIR
EXPORT_CSV = os.path.join(DATA_DIR, "spotify_export.csv")


def export_first_artists():
    """spotify_url -> first artist according to Spotify ("A, B" -> "A"; keeps names with "/")."""
    if not os.path.exists(EXPORT_CSV):
        return {}
    exp = pd.read_csv(EXPORT_CSV, encoding="utf-8-sig", dtype=str).fillna("")
    return {u: a.split(", ")[0].strip() for u, a in zip(exp["spotify_url"], exp["artist"]) if u and a}


def read_tags(file_path):
    """(title, first artist) from the ID3 tags spotdl writes; ("", "") if there are none."""
    try:
        audio = ID3(file_path)
    except Exception:
        return "", ""
    title = str(audio.get("TIT2", "")).strip()
    artist = str(audio.get("TPE1", "")).split("/")[0].split("\x00")[0].strip()
    return title, artist

def get_spotify_url_from_file(file_path):
    """Reads the Spotify URL spotdl stores in the WOAS ID3 tag."""
    try:
        audio = ID3(file_path)
        for key, frame in audio.items():
            if key.startswith('WOAS') and "open.spotify.com/track/" in frame.url:
                return frame.url.split("?")[0]
    except (ID3NoHeaderError, Exception):
        pass
    return ""

def upgrade_csv():
    print("Iniciando actualización y expansión del esquema de 'all_songs.csv'...")
    
    # 1. Load the existing CSV or create the base structure
    if os.path.exists(CSV_PATH):
        # sep=None detects ',' (original export) or ';' (this schema); always reading with ',' used to load
        # the migrated CSV as a single column, losing all its data
        df_old = pd.read_csv(CSV_PATH, sep=None, engine="python", encoding="utf-8-sig")
        print(f"Cargado CSV actual con {len(df_old)} registros.")
    else:
        df_old = pd.DataFrame()
        print("No se encontró CSV previo, se creará uno nuevo basado en los archivos locales.")

    # Final standard column names
    standard_columns = [
        "track_name", "artist_name", "spotify_url", "file_path",
        "genre", "bpm_exact", "bpm_range",
        "energy", "energy_group", "danceability", "danceability_group",
        "valence", "valence_group", "acousticness", "speechiness",
        "genre_family", "genre_auto", "genre_source", "genre_style", "key", "mode", "camelot",
        "sp_energy", "sp_danceability", "sp_valence", "sp_acousticness", "sp_speechiness",
    ]
    # Columns filled by enrich_spotify_metrics.py: only preserved here
    extra_columns = standard_columns[standard_columns.index("genre_family"):]

    # Find every local MP3 so all of them are included
    mp3_files = glob.glob(os.path.join(MUSIC_DIR, "**", "*.mp3"), recursive=True)
    # Without MP3s (folder unavailable, disk unmounted...) the CSV is not touched
    if not mp3_files or (not df_old.empty and len(mp3_files) < 0.5 * len(df_old)):
        print(f"Error: solo hay {len(mp3_files)} MP3 en {MUSIC_DIR} (el CSV tiene {len(df_old)}); no se actualiza.")
        sys.exit(1)
    
    first_artist = export_first_artists()
    rows = []
    for fpath in mp3_files:
        filename = os.path.basename(fpath)
        # Try to get artist and title from the file name (e.g. "Artists - Song.mp3")
        name_clean = os.path.splitext(filename)[0]
        parts = name_clean.split(" - ", 1)
        artist = parts[0] if len(parts) > 1 else "Unknown Artist"
        track = parts[1] if len(parts) > 1 else name_clean

        # Look it up in the old CSV to keep any custom data
        existing_row = None
        if not df_old.empty and "file_path" in df_old.columns:
            match = df_old[df_old["file_path"] == fpath]
            if not match.empty:
                existing_row = match.iloc[0]

        # BPM: computed by essentia_analyzer.py and written by enrich_spotify_metrics.py
        has_bpm = existing_row is not None and pd.notna(existing_row.get("bpm_exact"))
        bpm_exact = float(existing_row["bpm_exact"]) if has_bpm else pd.NA
        bpm_range = existing_row["bpm_range"] if has_bpm else "Unknown"

        # Name and artist: always from the MP3 tags (Spotify title and FIRST artist), so every row follows
        # the same rule. Without tags: whatever was there, or the file name
        tag_title, tag_artist = read_tags(fpath)
        url = existing_row["spotify_url"] if existing_row is not None and pd.notna(existing_row.get("spotify_url")) else get_spotify_url_from_file(fpath)
        tag_artist = first_artist.get(url) or tag_artist   # spotdl joins artists with "/": "AC/DC" would be split
        old_title = existing_row["track_name"] if existing_row is not None and pd.notna(existing_row.get("track_name")) else None
        old_artist = existing_row["artist_name"] if existing_row is not None and pd.notna(existing_row.get("artist_name")) else None
        row = {
            "track_name": tag_title or old_title or track,
            "artist_name": tag_artist or old_artist or artist.split(",")[0].strip(),
            "spotify_url": existing_row["spotify_url"] if existing_row is not None and pd.notna(existing_row.get("spotify_url")) else get_spotify_url_from_file(fpath),
            "file_path": fpath,
            "genre": existing_row["genre"] if existing_row is not None and pd.notna(existing_row.get("genre")) else "Unassigned",
            "bpm_exact": bpm_exact,
            "bpm_range": bpm_range,
            # Numeric metrics: empty if undefined (filled by enrich_spotify_metrics.py)
            "energy": int(existing_row["energy"]) if existing_row is not None and pd.notna(existing_row.get("energy")) else pd.NA,
            "energy_group": existing_row["energy_group"] if existing_row is not None and pd.notna(existing_row.get("energy_group")) else "Unknown",
            "danceability": int(existing_row["danceability"]) if existing_row is not None and pd.notna(existing_row.get("danceability")) else pd.NA,
            "danceability_group": existing_row["danceability_group"] if existing_row is not None and pd.notna(existing_row.get("danceability_group")) else "Unknown",
            "valence": int(existing_row["valence"]) if existing_row is not None and pd.notna(existing_row.get("valence")) else pd.NA,
            "valence_group": existing_row["valence_group"] if existing_row is not None and pd.notna(existing_row.get("valence_group")) else "Unknown",
            "acousticness": int(existing_row["acousticness"]) if existing_row is not None and pd.notna(existing_row.get("acousticness")) else pd.NA,
            "speechiness": int(existing_row["speechiness"]) if existing_row is not None and pd.notna(existing_row.get("speechiness")) else pd.NA,
        }
        for col in extra_columns:
            row[col] = existing_row[col] if existing_row is not None and pd.notna(existing_row.get(col)) else pd.NA
        rows.append(row)

    # New DataFrame with the full schema
    df_new = pd.DataFrame(rows, columns=standard_columns)
    for col in ["energy", "danceability", "valence", "acousticness", "speechiness"] + [c for c in extra_columns if c.startswith("sp_")]:
        df_new[col] = df_new[col].astype("Int64")  # integers, leaving missing ones empty
    tmp = CSV_PATH + ".tmp"
    df_new.to_csv(tmp, index=False, sep=';', encoding='utf-8-sig')  # BOM: Excel shows accents and ñ correctly
    os.replace(tmp, CSV_PATH)  # never leaves the CSV half written
    print(f"¡Esquema de 'all_songs.csv' actualizado con éxito! Total de pistas procesadas: {len(df_new)}")

if __name__ == "__main__":
    upgrade_csv()
