#!/bin/bash
# Full pipeline. Works by hand or unattended (cron/launchd): absolute paths, one log per run in
# logs/, a lock against simultaneous runs and a non-zero exit code if anything fails.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # project folder (this script is in scripts/)
# Personal settings (interpreters, Spotify Client ID): config.env, see config.example.env
if [ -f "$DIR/config.env" ]; then set -a; . "$DIR/config.env"; set +a; fi
PY="${PYTHON:-$DIR/.venv/bin/python}"                          # requirements.txt
PY_ESSENTIA="${PYTHON_ESSENTIA:-$DIR/.venv-essentia/bin/python}"  # requirements-essentia.txt + models

cd "$DIR/scripts" || exit 1   # data in data/, music and logs at the project root
export PATH="$(dirname "$PY"):$HOME/.spotdl:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Atomic lock (only one mkdir succeeds): if another run is alive, exit; if an orphan lock was
# left behind (dead process), take it over
mkdir -p "$DIR/data/locks"
LOCK="$DIR/data/locks/.runner.lock.d"
take_lock() { mkdir "$LOCK" 2>/dev/null && echo $$ > "$LOCK/pid"; }
if ! take_lock; then
    other=$(cat "$LOCK/pid" 2>/dev/null)
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        echo "Ya hay una ejecución en curso (PID $other). Saliendo."
        exit 1
    fi
    # No pid yet and just created: the other run is just starting
    age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
    if [ -z "$other" ] && [ "$age" -lt 10 ]; then
        echo "Otra ejecución está empezando. Saliendo."
        exit 1
    fi
    # Take over the orphan with mv (atomic): if two try at once, only one succeeds
    if mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null; then
        if [ "$(cat "$LOCK.stale.$$/pid" 2>/dev/null)" != "$other" ]; then
            # It was the new lock of another run that got there first: give it back and exit
            [ -e "$LOCK" ] || mv "$LOCK.stale.$$" "$LOCK" 2>/dev/null
            rm -rf "$LOCK.stale.$$" 2>/dev/null
            echo "Otra ejecución acaba de empezar. Saliendo."
            exit 1
        fi
        rm -rf "$LOCK.stale.$$"
    fi
    take_lock || { echo "Otra ejecución acaba de empezar. Saliendo."; exit 1; }
fi
trap 'rm -rf "$LOCK"' EXIT

# The log is created after taking the lock: a run that exits with "already running" leaves no log
# (otherwise the phone would take it for the "last update")
mkdir -p "$DIR/logs"
LOG="$DIR/logs/runner_$(date +%Y-%m-%d_%H%M%S).log"
# Keep only the last 30 run logs
ls -t "$DIR"/logs/runner_*.log 2>/dev/null | tail -n +30 | while read -r old; do rm -f "$old"; done
exec > >(tee -a "$LOG") 2>&1
# If the runner is stopped (kill / Ctrl-C), do not leave its steps running on their own
# (only its children: "kill 0" would also kill the Mac agent, which shares the process group)
trap 'trap - TERM INT; pkill -TERM -P $$ 2>/dev/null; exit 143' TERM INT

FAILED=()
LOGIN_NEEDED=0
step() {  # step <name> <command...>
    local name=$1; shift
    echo; echo "== $(date '+%F %T') $name"
    "$@"
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo "!! $name falló (código $rc)"
        FAILED+=("$name")
    fi
    return $rc
}

# 1. Spotify list (top 4 weeks, 6 months, all time + Liked Songs). Optional: without a saved login
#    (exit code 2) the previous spotify_export.csv is used
"$PY" spotify_export.py
rc=$?
if [ $rc -ne 0 ]; then
    echo "Aviso: spotify_export.csv no se ha actualizado (código $rc); se usa el anterior"
    if [ $rc -eq 2 ]; then LOGIN_NEEDED=1; else FAILED+=("spotify_export"); fi
fi

# 1b. Cleanup: only right after a fresh export. Moves to the trash the MP3s no longer in any
#     list; if its safety checks fail (code 3) it only warns
if [ $rc -eq 0 ]; then
    echo; echo "== $(date '+%F %T') Limpieza"
    "$PY" library_cleanup.py
    crc=$?
    if [ $crc -eq 3 ]; then
        echo "Aviso: limpieza omitida por seguridad (ver arriba)"
    elif [ $crc -ne 0 ]; then
        echo "!! Limpieza falló (código $crc)"
        FAILED+=("Limpieza")
    fi
fi

# 2-3. Downloads and analysis: failures are recorded, but the run goes on with the MP3s we have.
#      Versions: MP3s that did not come from the official studio track (live, music videos,
#      covers, karaoke...) are replaced with it when it exists (only new ones; see version_audit.py)
step "Descargas" "$PY" download_manager.py
step "Versiones" "$PY" version_audit.py
# Loudness: evens out new MP3s without re-encoding (see loudness.py)
step "Volumen" "$PY" loudness.py
step "Análisis Essentia" "$PY_ESSENTIA" essentia_analyzer.py

# 4-5. CSV: enrichment only runs if the schema step succeeded
if step "Esquema all_songs.csv" "$PY" update_csv_schema.py; then
    step "Métricas y géneros" "$PY" enrich_spotify_metrics.py && \
        step "Playlists" "$PY" playlist_builder.py --all
fi
# 6. "MP3 Player Card" folder (MP3 player): MP3s with ASCII names + one .m3u per playlist
step "MP3 Player Card" "$PY" mp3_export.py

echo
if [ ${#FAILED[@]} -eq 0 ] && [ $LOGIN_NEEDED -eq 1 ]; then
    # Code 2: everything else went fine, but the Spotify list is frozen until you log in
    echo "== $(date '+%F %T') Terminado, pero hace falta iniciar sesión en Spotify: ejecuta a mano"
    echo "   cd \"$DIR/scripts\" && \"$PY\" spotify_export.py   Log: $LOG"
    exit 2
elif [ ${#FAILED[@]} -eq 0 ]; then
    echo "== $(date '+%F %T') Terminado sin errores. Log: $LOG"
else
    echo "== $(date '+%F %T') Terminado con errores en: ${FAILED[*]}. Log: $LOG"
    [ $LOGIN_NEEDED -eq 1 ] && echo "Aviso: además hace falta iniciar sesión en Spotify (spotify_export.py a mano)"
    exit 1
fi
