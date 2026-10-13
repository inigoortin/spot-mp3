"""
Builds playlists from all_songs.csv.

Criteria (dict column -> value):
  "genre": "Spanish Pop"                      equals (case-insensitive)
  "genre": ["Spanish Pop", "Spanish Indie"]   any of the list
  "genre_not": "Spanish Rap"                  exclude (value or list)
  "energy_min": 60, "bpm_exact_max": 130      minimums / maximums
  "valence": 30, "bpm_exact": 124             a number on a numeric column is a target;
                                              sorted from most to least similar

Useful columns: genre, genre_family (Pop, Indie, Urban, Rap, Rock...), energy_group /
danceability_group (Low, Medium, High), valence_group (Dark/Sad, Neutral, Happy/Upbeat),
energy, danceability, valence, acousticness, speechiness (0-100), bpm_exact, bpm_range,
key, mode (major/minor), camelot.

Each playlist is saved as Playlists/<name>.m3u8 (opens in Music, VLC, etc.).

  python playlist_builder.py                       list the example playlists
  python playlist_builder.py "Spanish Pop Sad"     build one
  python playlist_builder.py --all                 rebuild the examples and those made with
                                                   --create, keeping songs removed or added by
                                                   hand from the phone
  python playlist_builder.py --create "Indie Tranquilo" --criteria '{"genre": "Spanish Indie"}'
                             [--limit 40] [--order-by bpm_exact]
"""

import json
import os
import re
import sys
import unicodedata

import pandas as pd

import playlist_store

CSV_PATH = playlist_store.SONGS_CSV     # absolute paths: works from any folder

PRESETS = {
    "Spanish Pop Sad": {
        # "Spanish" = sung in Spanish or Catalan: pop/indie from Spain, Latin America and Catalonia
        "criteria": {"genre": ["Spanish Pop", "Spanish Indie", "Latin Pop", "Catalan Pop"], "valence_group": "Dark/Sad",
                     "valence": 30, "energy": 30},
        "limit": 40,
    },
    "Fiesta Urbana": {
        "criteria": {"genre_family": "Urban", "energy_group": "High", "danceability_min": 90},
        "limit": 50,
        "order_by": "bpm_exact",
    },
    "Acústico en Castellano": {
        "criteria": {"genre": ["Spanish Pop", "Spanish Indie", "Latin Pop"], "acousticness_min": 70},
        "limit": 40,
        "order_by": "-acousticness",
    },
    "English Pop Feel Good": {
        "criteria": {"genre": ["Pop", "Indie Pop"], "valence_group": "Happy/Upbeat", "danceability_min": 70},
        "limit": 40,
    },
    "Rap en Castellano": {
        "criteria": {"genre": ["Spanish Rap", "Spanish Urban"], "speechiness_min": 30},
        "limit": 40,
        "order_by": "-speechiness",
    },
    "Gym 125+ BPM": {
        "criteria": {"energy_group": "High", "bpm_exact_min": 125, "bpm_exact_max": 150,
                     "danceability_min": 80},
        "limit": 50,
        "order_by": "bpm_exact",
    },
    "Reggaetón para Mezclar": {
        # ~95 BPM and sorted by key (Camelot) for harmonic transitions
        "criteria": {"genre": ["Urbano Latino", "Spanish Urban"], "bpm_exact_min": 88,
                     "bpm_exact_max": 102, "danceability_min": 85},
        "limit": 40,
        "order_by": "camelot",
    },
}


def load_songs():
    if not os.path.exists(CSV_PATH):
        print(f"Error: No se encuentra el archivo {CSV_PATH}")
        return pd.DataFrame()
    df = pd.read_csv(CSV_PATH, sep=";", encoding="utf-8-sig")
    # Without an MP3 on disk a song does not enter a playlist (e.g. one removed during review while
    # another step rewrote all_songs.csv with the row still in it)
    return df[df["file_path"].map(lambda p: isinstance(p, str) and os.path.exists(p))]


def _as_list(value):
    return value if isinstance(value, (list, tuple, set)) else [value]


def _matches(series, value):
    """Equality / membership in a list, case-insensitive for text."""
    values = _as_list(value)
    if pd.api.types.is_numeric_dtype(series):
        return series.isin(values)
    return series.fillna("").str.lower().isin([str(v).lower() for v in values])


def _song_key(artist, title):
    """Same song even if the title's "(ft. ...)" / "- ft. ..." changes."""
    title = re.split(r"\s[\(\[-]\s?(?:ft|feat|with|con)\b|\s\((?:ft|feat|with|con)\b", str(title), flags=re.I)[0]
    text = unicodedata.normalize("NFKD", f"{artist}|{title}".lower())
    return re.sub(r"[^a-z0-9|]", "", "".join(c for c in text if not unicodedata.combining(c)))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def criteria_problems(criteria, df, limit=None):
    """
    List of errors of some criteria against the library columns (empty if fine). So a misspelt
    criterion does not produce a playlist with the whole library.
    """
    if not isinstance(criteria, dict):
        return ["los criterios deben ser un objeto"]
    problems = []
    for key, value in criteria.items():
        suffix = key[-4:] if key.endswith(("_min", "_max", "_not")) else ""
        col = key[:-4] if suffix else key
        if col not in df.columns:
            problems.append(f"criterio desconocido: '{key}'")
            continue
        numeric = pd.api.types.is_numeric_dtype(df[col])
        values = value if isinstance(value, list) else [value]
        if suffix in ("_min", "_max"):
            if not (numeric and _is_number(value)):
                problems.append(f"'{key}' necesita un número")
        elif numeric and not all(_is_number(v) for v in values):
            problems.append(f"'{key}' necesita números")
        elif not numeric and not all(isinstance(v, str) for v in values):
            problems.append(f"'{key}' necesita texto")
        elif not values:
            problems.append(f"'{key}' está vacío")
    if limit is not None and not (isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 1000):
        problems.append("el número de canciones debe estar entre 1 y 1000")
    return problems


def _camelot_key(code):
    """'8A' -> (8, 'A') to sort along the Camelot wheel."""
    code = str(code)
    return (int(code[:-1]), code[-1]) if code[:-1].isdigit() else (99, "")


def build_playlist(criteria, playlist_name="playlist_generada", limit=50, sort_by_proximity=True,
                   export=True, order_by=None, export_txt=None, source=None, excluded=(), pinned=(),
                   new=False):
    """
    Filters all_songs.csv with `criteria`, sorts by closeness to the numeric targets, applies
    `limit`, re-sorts by `order_by` (column; "-col" = descending; "camelot" follows the harmonic
    wheel) and saves Playlists/<name>.m3u8.
    `excluded` (ids removed by hand) never enter: the next closest song takes their place.
    `pinned` (ids added by hand) always enter, whether or not they match.
    `new`: a new playlist or one recreated by hand: its manual changes start empty and it is
    restored if it was deleted. When rebuilding (new=False) the registry keeps its changes.
    `export_txt` is kept for compatibility (same as `export`).
    """
    if export_txt is not None:
        export = export_txt
    library = load_songs()
    if library.empty:
        return library
    library["track_id"] = library["spotify_url"].map(playlist_store.track_id)
    excluded, pinned = set(excluded), [t for t in pinned if t not in set(excluded)]
    problems = criteria_problems(criteria, library, limit)
    if problems:
        raise ValueError("; ".join(problems))
    # Song key; if the title has no Latin letters (e.g. Korean or Japanese), the id is used so all such titles of
    # one artist are not taken for the same song
    library["_key"] = [k + tid if k.endswith("|") else k
                       for k, tid in zip((_song_key(a, t) for a, t in zip(library["artist_name"], library["track_name"])),
                                         library["track_id"])]
    # Also exclude other downloaded versions of the same song (another Spotify id)
    hidden = set(library.loc[library["track_id"].isin(excluded | set(pinned)), "_key"])
    df = library[~library["_key"].isin(hidden)]

    targets = {}
    # 1. Filters
    for key, value in criteria.items():
        if key.endswith("_min") and key[:-4] in df.columns:
            df = df[df[key[:-4]] >= value]
        elif key.endswith("_max") and key[:-4] in df.columns:
            df = df[df[key[:-4]] <= value]
        elif key.endswith("_not") and key[:-4] in df.columns:
            df = df[~_matches(df[key[:-4]], value)]
        elif key in df.columns:
            # A number on a numeric field is a target to sort by closeness
            if sort_by_proximity and pd.api.types.is_numeric_dtype(df[key]) and isinstance(value, (int, float)):
                targets[key] = value
                continue
            df = df[_matches(df[key], value)]
        else:
            print(f"Aviso: la columna '{key}' no existe; criterio ignorado")

    # 2. Sort by closeness to the numeric targets
    if targets and not df.empty:
        # Each distance relative to its column's range: otherwise BPM (60-200) would outweigh a 0-100 value
        span = {col: max(float(library[col].max() - library[col].min()), 1.0) for col in targets}
        distance = sum((df[col] - target).abs() / span[col] for col, target in targets.items())
        df = df.assign(_distance=distance).sort_values("_distance").drop(columns="_distance")

    # A single version of each song (a few were downloaded twice under another title)
    df = df[~df["_key"].duplicated()]
    total_matches = len(df)

    # 3. Limit (songs added by hand do not count) and songs added by hand
    if limit and total_matches > limit:
        df = df.head(limit)
    extra = library[library["track_id"].isin(pinned)].drop_duplicates("track_id")
    df = pd.concat([df, extra])

    # 4. Final playback order
    if order_by and not df.empty:
        descending = order_by.startswith("-")
        col = order_by.lstrip("-")
        if col == "camelot":
            df = (df.assign(_k=df["camelot"].map(_camelot_key))
                    .sort_values(["_k", "bpm_exact"], ascending=not descending).drop(columns="_k"))
        elif col in df.columns:
            df = df.sort_values(col, ascending=not descending)

    print(f"\n[Playlist Builder] '{playlist_name}': {total_matches} canciones cumplen los criterios"
          + (f", se usan las {len(df) - len(extra)} más afines" if len(df) - len(extra) < total_matches else "")
          + (f" (+{len(extra)} añadidas a mano)" if len(extra) else "")
          + (f" ({len(excluded)} quitadas a mano)" if excluded else ""))

    # 5. Export to M3U8 (and record it in the playlist registry)
    if export and not df.empty:
        rows = [{"artist": r["artist_name"], "title": r["track_name"], "file_path": r["file_path"]}
                for _, r in df.iterrows()]
        output_file = playlist_store.write_local(playlist_name, rows)
        fields = dict(source=source or "custom", criteria=criteria, limit=limit, order_by=order_by)
        if new:
            fields.update(excluded=sorted(excluded), pinned=list(pinned), was_pinned=[], deleted=False)
        playlist_store.register(playlist_name, **fields)
        print(f"-> Guardada en '{os.path.relpath(output_file)}'")

    return df


def build_preset(name, excluded=(), pinned=(), new=False):
    preset = PRESETS[name]
    return build_playlist(preset["criteria"], playlist_name=name, limit=preset.get("limit", 50),
                          order_by=preset.get("order_by"), source="preset", excluded=excluded, pinned=pinned, new=new)


def rebuild(name):
    """
    Rebuilds a registry playlist with its criteria and manual changes. Everything happens inside the
    store's lock: an edit or deletion from the phone arriving at the same time waits and is applied
    afterwards, never lost and never resurrecting the playlist. Returns None if skipped.
    """
    with playlist_store.locked:
        entry = playlist_store.load().get(name)
        if name not in PRESETS and not entry:
            return None   # deleted in the meantime
        entry = entry or {}
        if entry.get("deleted") or entry.get("source") == "spotify":
            return None   # Spotify ones are built in build_spotify_playlists (they have no criteria)
        excluded, pinned = entry.get("excluded") or [], entry.get("pinned") or []
        try:
            if name in PRESETS:
                result = build_preset(name, excluded, pinned, new=not entry)
            else:
                result = build_playlist(entry.get("criteria") or {}, playlist_name=name, limit=entry.get("limit") or 50,
                                        order_by=entry.get("order_by"), source="custom", excluded=excluded, pinned=pinned)
        except ValueError as e:
            print(f"\n[Playlist Builder] '{name}': criterios no válidos ({e}); se deja como está")
            return None
        if result.empty and playlist_store.exists(name):
            playlist_store.write_local(name, [])   # no song matches the criteria any more
        return result


def _spotify_local_name(pid, sub, registry):
    """Local name of a chosen Spotify playlist: its own, or with " (Spotify)" if it clashes with another."""
    current = sub.get("local_name")
    if current and registry.get(current, {}).get("spotify_id") == pid:
        return current
    base = (sub.get("name") or pid).strip()
    for c in playlist_store.FORBIDDEN_CHARS:   # "24/7" -> "24-7": those characters break paths and the app
        base = base.replace(c, "-")
    base = base.strip(" -") or pid
    for candidate in (base, f"{base} (Spotify)", f"{base} (Spotify {pid[:5]})"):
        if registry.get(candidate, {}).get("spotify_id") == pid or playlist_store.name_problem(candidate) is None:
            return candidate
    return None


def build_spotify_playlists():
    """
    Local playlists for the chosen Spotify playlists (spotify_playlists.py), in Spotify's order, with
    the songs already downloaded. They respect what was removed/added by hand.
    """
    import spotify_playlists as spl
    library = load_songs()
    if library.empty:
        return
    library["track_id"] = library["spotify_url"].map(playlist_store.track_id)
    by_id = {tid: r for tid, r in zip(library["track_id"], library.to_dict("records")) if tid}
    by_key = {_song_key(r["artist_name"], r["track_name"]): r for r in library.to_dict("records")}
    for pid, sub in spl.chosen().items():
        tracks = sub.get("tracks")
        if not tracks:
            continue   # not exported yet (filled in by the next spotify_export.py)
        with playlist_store.locked:
            if pid not in spl.chosen():
                continue   # download stopped in the meantime: not recreated
            registry = playlist_store.load()
            name = _spotify_local_name(pid, sub, registry)
            if not name:
                print(f"\n[Playlist Builder] Spotify '{sub.get('name')}': no hay un nombre libre; se omite")
                continue
            if name != sub.get("local_name"):
                spl.set_local_name(pid, name)
            entry = registry.get(name, {})
            excluded, pinned = set(entry.get("excluded") or []), list(entry.get("pinned") or [])
            rows, seen = [], set()
            for t in tracks:
                row = by_id.get(playlist_store.track_id(t["url"])) or \
                    by_key.get(_song_key(str(t["artist"]).split(",")[0].strip(), t["title"]))
                if row is None or row["track_id"] in excluded or row["track_id"] in seen:
                    continue   # not downloaded yet, removed by hand or repeated
                seen.add(row["track_id"])
                rows.append(row)
            rows += [by_id[t] for t in pinned if t in by_id and t not in seen]
            playlist_store.write_local(name, [{"artist": r["artist_name"], "title": r["track_name"],
                                               "file_path": r["file_path"]} for r in rows])
            fields = dict(source="spotify", spotify_id=pid, criteria={}, limit=None, order_by=None)
            if not entry:
                fields.update(excluded=[], pinned=[], was_pinned=[], deleted=False)
            playlist_store.register(name, **fields)
        print(f"\n[Playlist Builder] Spotify '{name}': {len(rows)} canciones"
              + (f" ({len(tracks) - len(seen)} aún sin descargar o quitadas)" if len(seen) < len(tracks) else ""))


def rebuild_all():
    """Rebuilds the examples and those created from the phone (except deleted ones)."""
    if load_songs().empty:
        # Without a library (CSV missing or empty) playlists are not emptied
        print("Error: all_songs.csv no está disponible o está vacío; no se regenera nada.")
        sys.exit(1)
    registry = playlist_store.load()
    names = list(PRESETS) + [n for n, e in registry.items() if n not in PRESETS and e.get("source") == "custom"]
    for name in names:
        rebuild(name)
    build_spotify_playlists()

def _option(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return default


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Playlists de ejemplo (python3 playlist_builder.py \"<nombre>\" o --all):")
        for name, preset in PRESETS.items():
            print(f"  - {name}: {preset['criteria']}")
    elif args == ["--all"]:
        rebuild_all()
    elif args[0] == "--create":
        name = args[1]
        rest = args[2:]
        criteria = json.loads(_option(rest, "--criteria", "{}"))
        limit = int(_option(rest, "--limit", 50))
        order_by = _option(rest, "--order-by")
        with playlist_store.locked:
            problem = playlist_store.name_problem(name)
            if problem:
                print(f"No se ha creado: {problem}.")
                sys.exit(1)
            try:
                result = build_playlist(criteria, playlist_name=name, limit=limit, order_by=order_by, source="custom",
                                        new=True)
            except ValueError as e:
                print(f"No se ha creado: {e}.")
                sys.exit(1)
        if result.empty:
            print("Ninguna canción cumple los criterios; no se ha creado la playlist.")
            sys.exit(1)
    else:
        for name in args:
            if name not in PRESETS:
                print(f"No existe la playlist '{name}'. Opciones: {', '.join(PRESETS)}")
                continue
            with playlist_store.locked:
                clash = next((n for n in playlist_store.load()
                              if n != name and playlist_store.file_key(n) == playlist_store.file_key(name)), None)
                if clash:
                    print(f"No se crea '{name}': su fichero es el de la playlist '{clash}'. Bórrala o renómbrala antes.")
                    continue
                entry = playlist_store.load().get(name, {})
                if entry.get("deleted"):
                    # Creating it by hand restores it (without the earlier changes)
                    result = build_preset(name, new=True)
                else:
                    result = build_preset(name, entry.get("excluded") or [], entry.get("pinned") or [], new=not entry)
            if not result.empty:
                print(result[["artist_name", "track_name", "genre", "valence", "energy", "bpm_exact", "camelot"]]
                      .head(8).to_string(index=False))
