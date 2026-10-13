"""
Uploads playlists to your Spotify account as PRIVATE playlists.

Source: Playlists/<name>.m3u8 from playlist_builder.py (each MP3 is looked up in all_songs.csv by
its path to get its Spotify link). A playlist previously created by this script (marked in its
description) gets its songs replaced; playlists you made by hand are never touched, even with the
same name (a new one is created instead).

  python spotify_upload.py                      list the available playlists
  python spotify_upload.py "Spanish Pop Sad"    upload one (or several)
  python spotify_upload.py --all                upload all
  add --dry-run to see what would be uploaded without touching Spotify

The first upload must be run by hand to approve the playlist permissions in the browser.
"""

import os
import sys
from datetime import date

import playlist_store
from spotify_auth import SCOPES_PLAYLISTS, get_client

MARKER = "[spotify-music]"  # marker in the description of playlists created by this script
BATCH = 100                 # maximum songs per API request


def track_uris(name):
    """Spotify URIs of the playlist, in order, and the songs that could not be resolved."""
    lib = playlist_store.songs()
    by_path = dict(zip(lib["file_path"], lib["spotify_url"]))
    uris, missing = [], []
    for path in playlist_store.read_paths(name):
        url = by_path.get(path)
        if isinstance(url, str) and "open.spotify.com/track/" in url:
            uri = "spotify:track:" + playlist_store.track_id(url)
            if uri not in uris:
                uris.append(uri)
        else:
            missing.append(os.path.basename(path))
    return uris, missing


def find_own_playlist(sp, name):
    """
    Own playlist created by this script: first the one from the link saved in the registry (even if
    renamed in Spotify), otherwise the one with that name (or None).
    """
    me = sp.me()["id"]
    saved = playlist_store.track_id(playlist_store.entry(name).get("spotify_url"))
    if saved:
        try:
            pl = sp.playlist(saved, fields="id,name,owner,description")
        except Exception:
            pl = None   # no access or invalid id: look it up by name
        if pl and pl["owner"]["id"] == me and MARKER in (pl.get("description") or ""):
            # Deleting your own playlist in Spotify only unfollows it: then a new one is created.
            # (spotipy 2.26's playlist_is_following is broken; me/library/contains is used instead)
            try:
                following = bool(sp.current_user_saved_items([f"spotify:playlist:{saved}"])[0])
            except Exception:
                following = True   # if it cannot be checked, better reuse it than duplicate it
            if following:
                return pl
    page = sp.current_user_playlists(limit=50)
    while page:
        for pl in page["items"]:
            if pl and pl["name"] == name and pl["owner"]["id"] == me and MARKER in (pl.get("description") or ""):
                return pl
        page = sp.next(page) if page.get("next") else None
    return None


def upload(sp, name, uris):
    description = f"Creada con playlist_builder el {date.today():%d/%m/%Y} {MARKER}"
    existing = find_own_playlist(sp, name)
    if existing:
        playlist_id = existing["id"]
        sp.playlist_change_details(playlist_id, public=False, description=description)
        action = "actualizada"
    else:
        playlist_id = sp.current_user_playlist_create(name, public=False, description=description)["id"]
        action = "creada"
    # Replace the contents: first chunk with replace, the rest is added
    sp.playlist_replace_items(playlist_id, uris[:BATCH])   # [] leaves it empty
    for start in range(BATCH, len(uris), BATCH):
        sp.playlist_add_items(playlist_id, uris[start:start + BATCH])
    # Spotify sometimes ignores public=False on creation: ask again and check
    sp.playlist_change_details(playlist_id, public=False)
    if sp.playlist(playlist_id, fields="public").get("public"):
        action += " (AVISO: Spotify la marca como pública; revísalo en la app: ⋯ > Hacer privada)"
    return action, f"https://open.spotify.com/playlist/{playlist_id}"


def main(args):
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    sources = playlist_store.all_names()
    if not args:
        print("Playlists disponibles (python3 spotify_upload.py \"<nombre>\" | --all, opcional --dry-run):")
        for name in sources:
            print(f"  - {name}")
        return

    names = list(sources) if args == ["--all"] else args
    unknown = [n for n in names if n not in sources]
    if unknown:
        print(f"No existen: {', '.join(unknown)}. Opciones: {', '.join(sources)}")
        names = [n for n in names if n in sources]

    sp = None if dry_run else get_client(SCOPES_PLAYLISTS, "spotify_upload.py")
    failed = []
    for name in names:
        uris, missing = track_uris(name)
        note = f" ({len(missing)} sin enlace de Spotify: {', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})" if missing else ""
        if dry_run:
            print(f"[dry-run] '{name}': se subirían {len(uris)} canciones como playlist privada{note}")
            continue
        if not uris and not playlist_store.entry(name).get("spotify_url"):
            print(f"'{name}': sin canciones que subir{note}")
            failed.append(name)
            continue
        try:
            # Empty locally but already published: empty it in Spotify too (otherwise it keeps the old songs)
            action, url = upload(sp, name, uris)
        except Exception as e:
            print(f"'{name}': ERROR al subir ({e}); la de Spotify puede haber quedado a medias, vuelve a publicarla")
            failed.append(name)
            continue
        with playlist_store.locked:
            if playlist_store.exists(name):   # it may have been deleted from the phone while uploading
                playlist_store.register(name, spotify_url=url, published=playlist_store.now())
        print(f"'{name}': {action} con {len(uris)} canciones (privada) -> {url}{note}")
    return 1 if failed or unknown else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
