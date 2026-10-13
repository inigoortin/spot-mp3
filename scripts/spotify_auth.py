"""
Shared Spotify login (PKCE: needs only the app's Client ID, no Client Secret).

Each script asks only for the scopes it uses. If the saved login lacks them (e.g. the first time a
playlist is uploaded), run that script by hand once to approve them in the browser; after that
the login renews itself.
"""

import os
import sys

import spotipy
from spotipy.oauth2 import SpotifyOauthError, SpotifyPKCE
from common import PRIVATE_DIR, SCRIPTS_DIR, config

HERE = SCRIPTS_DIR
# The Client ID identifies your Spotify app (not a secret): SPOTIFY_CLIENT_ID in config.env
CLIENT_ID = config("SPOTIFY_CLIENT_ID") or config("SPOTIPY_CLIENT_ID")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
TOKEN_CACHE = os.path.join(PRIVATE_DIR, ".spotify_token_cache")

SCOPES_READ = "user-top-read user-library-read"
SCOPES_PLAYLISTS = "playlist-read-private playlist-modify-private"
# The login is saved with every scope approved so far, so one script does not "lose" another's
# scopes when logging in again
ALL_SCOPES = f"{SCOPES_READ} {SCOPES_PLAYLISTS}"


def get_client(scopes, script_name):
    """
    Spotify client with at least `scopes`. In an unattended run without a valid login it exits with
    code 2 instead of waiting for the browser.
    """
    if not CLIENT_ID:
        print("Falta SPOTIFY_CLIENT_ID: copia config.example.env a config.env y pon el Client ID de tu app "
              "de Spotify (ver README).")
        sys.exit(2)
    interactive = sys.stdin.isatty()
    needed = SpotifyPKCE(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, scope=scopes,
                         cache_path=TOKEN_CACHE, open_browser=False)
    try:
        valid = needed.validate_token(needed.cache_handler.get_cached_token())
    except SpotifyOauthError as e:
        # Refresh token revoked or expired (e.g. the app's access was removed in Spotify)
        print(f"Spotify: el login guardado ya no vale ({e}).")
        valid = None
    except Exception as e:
        # No network: not a login problem, reported as a normal error
        print(f"Spotify: no se pudo renovar el login ({e}).")
        sys.exit(1)
    if valid:
        # The saved login already covers what is needed: use it as is (it renews itself)
        return spotipy.Spotify(auth_manager=needed)
    if not interactive:
        print(f"Spotify: falta iniciar sesión o aprobar permisos ({scopes}). "
              f"Ejecuta 'python3 {script_name}' a mano una vez.")
        sys.exit(2)
    # By hand: open the browser asking for every scope at once
    auth = SpotifyPKCE(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI, scope=ALL_SCOPES,
                       cache_path=TOKEN_CACHE, open_browser=True)
    # Without the cache: if the saved login is revoked, spotipy would try to renew it again and fail
    # before opening the browser. This way it always opens and saves the new one
    try:
        auth.get_access_token(check_cache=False)
    except SpotifyOauthError as e:
        print(f"Spotify: no se completó el inicio de sesión ({e}). Vuelve a ejecutar 'python3 {script_name}'.")
        sys.exit(2)
    except OSError as e:
        # Port 8888 is usually busy because of another unfinished login
        print(f"Spotify: no se pudo abrir el inicio de sesión ({e}). Cierra otros logins pendientes y vuelve a probar.")
        sys.exit(2)
    except KeyboardInterrupt:
        print("Inicio de sesión cancelado.")
        sys.exit(2)
    return spotipy.Spotify(auth_manager=auth)
