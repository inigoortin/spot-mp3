"""
Helpers shared by all scripts, so there are no diverging copies.

Standard library only and Python 3.10 compatible: essentia_analyzer.py (run with .venv-essentia)
imports it too. enrich_spotify_metrics.norm is intentionally different from norm() (it keeps
accents and other alphabets); title_key() lives in youtube_source.py.
"""

import json
import os
import re
import subprocess
import sys
import unicodedata

# Project folders: code in scripts/, generated data in data/; music, playlists, logs and models
# stay at the root (all_songs.csv and essentia_features.json store absolute paths to "MP3 Songs/")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
DATA_DIR = os.path.join(ROOT, "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")       # external-service caches and covers (rebuildable)
PRIVATE_DIR = os.path.join(DATA_DIR, "private")   # secrets: phone tokens and Spotify login
LOCKS_DIR = os.path.join(DATA_DIR, "locks")       # inter-process locks (runner, export...)
for _d in (CACHE_DIR, PRIVATE_DIR, LOCKS_DIR):
    os.makedirs(_d, exist_ok=True)

CONFIG_PATH = os.path.join(ROOT, "config.env")    # personal settings (see config.example.env)


def config(key, default=None):
    """Setting from the environment or, failing that, from config.env (KEY=value lines)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            for line in f:
                name, sep, value = line.strip().partition("=")
                if sep and not name.startswith("#") and name.strip() == key:
                    return value.strip().strip("\"'") or default
    except FileNotFoundError:
        pass
    return default


def install_launch_agent(label, script, log_name, **options):
    """
    Writes ~/Library/LaunchAgents/<label>.plist running `script` with this same Python and loads it
    (replacing a previous one). Returns (returncode, stderr) of launchctl bootstrap.
    """
    import plistlib
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    log = os.path.join(ROOT, "logs", log_name)
    os.makedirs(os.path.dirname(log), exist_ok=True)
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    # launchd does not expand ~ or $HOME: full paths. spotdl's ffmpeg lives in ~/.spotdl;
    # /usr/sbin and /sbin hold diskutil and ifconfig
    path = ":".join([os.path.dirname(sys.executable), os.path.expanduser("~/.spotdl"),
                     "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    plist = {"Label": label, "ProgramArguments": [sys.executable, os.path.abspath(script)],
             "WorkingDirectory": SCRIPTS_DIR, "EnvironmentVariables": {"PATH": path},
             "StandardOutPath": log, "StandardErrorPath": log, **options}
    import time
    service = f"gui/{os.getuid()}/{label}"
    subprocess.run(["launchctl", "bootout", service], capture_output=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)
    # bootout returns before the old service is gone; bootstrapping too early fails with error 5
    for _ in range(20):
        out = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path],
                             capture_output=True, text=True)
        if out.returncode == 0:
            break
        time.sleep(0.5)
    return out.returncode, out.stderr.strip()


def uninstall_launch_agent(label):
    """Stops and removes ~/Library/LaunchAgents/<label>.plist."""
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    if os.path.exists(plist_path):
        os.remove(plist_path)


def norm(s):
    """Lowercase, without accents, punctuation or spaces: for comparing titles and artists."""
    s = unicodedata.normalize("NFKD", "" if s is None else str(s).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s)


def track_id(url):
    """Spotify id of a link (without trailing '/' or '?si=...'); "" if there is no link."""
    return str(url).rstrip("/").split("/")[-1].split("?")[0] if isinstance(url, str) and url else ""


def load_json(path, default=None, strict=False, warn=None):
    """
    Reads a JSON file. Missing → `default`. Corrupt or not of `default`'s type (e.g. a list instead
    of a dict): with strict=True raises ValueError (files that must never be overwritten, like
    tokens or jobs); otherwise calls `warn(message)` if given and returns `default`.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        if strict:
            raise
        if warn:
            warn(f"Aviso: {os.path.basename(path)} estaba dañado; se ignora")
        return default
    if default is not None and not isinstance(data, type(default)):
        if strict:
            raise ValueError(f"{os.path.basename(path)}: se esperaba {type(default).__name__}")
        if warn:
            warn(f"Aviso: {os.path.basename(path)} no tiene el formato esperado; se ignora")
        return default
    return data


def save_json(path, data, sort_keys=False, private=False):
    """Atomic UTF-8 write (an interruption never leaves the file unreadable).
    private=True makes it readable by the user only (0600), for secrets."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=sort_keys)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
