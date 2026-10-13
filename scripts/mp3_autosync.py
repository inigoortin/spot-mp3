"""
Syncs the MP3 player when it is plugged into the Mac.

A LaunchAgent (com.spotmp3.mp3sync, StartOnMount) runs it whenever a disk is mounted. If the
disk is your player, a Mac dialog asks whether to sync it; if yes, it updates "MP3 Player Card",
copies new or changed files to the card and removes what is gone (songs removed by the cleanup or
by hand, deleted playlists). At the end it offers to eject it.

- Known player: recognised by the volume UUID (data/mp3_devices.json).
- A new disk that looks like a player (external, FAT32/exFAT, MP3 or .m3u in its root): it asks
  whether it is your player; "No es mi MP3" is remembered so it does not ask again.
- It asks only once per connection (volume UUID + the time macOS created its /dev/diskN).
- mp3_export.py's protections still apply: if the export has far fewer songs than the card,
  nothing is deleted and you are told.

    python mp3_autosync.py              check the mounted disks (the LaunchAgent does this)
    python mp3_autosync.py --install    install and load the LaunchAgent
    python mp3_autosync.py --uninstall  remove it
    python mp3_autosync.py --forget     forget known and dismissed players
"""

import fcntl
import traceback
import os
import plistlib
import subprocess
import sys
from datetime import datetime

from common import LOCKS_DIR, SCRIPTS_DIR, DATA_DIR, install_launch_agent, load_json, save_json, uninstall_launch_agent

HERE = SCRIPTS_DIR
DEVICES_PATH = os.path.join(DATA_DIR, "mp3_devices.json")
STATE_PATH = os.path.join(DATA_DIR, ".mp3_autosync_state.json")
LOCK_PATH = os.path.join(LOCKS_DIR, ".mp3_autosync.lock")
LABEL = "com.spotmp3.mp3sync"
TITLE = "Mi Música · MP3"
PLAYER_FS = ("msdos", "exfat", "fat")       # FAT32 / exFAT (what player cards use)
MIN_MEDIA_FILES = 5                        # an unknown disk "looks like" a player if it has this in its root
DIALOG_TIMEOUT = 600                       # s: if nobody answers, it counts as "Ahora no"
PERMISSION_HELP = ("macOS no deja a la sincronización leer el MP3. Ve a Ajustes del Sistema > Privacidad y "
                   "seguridad > Archivos y carpetas (o Acceso total al disco) y permite el acceso a "
                   "'Volúmenes extraíbles' para python; luego desconecta y vuelve a conectar el MP3.")


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- Mac dialogs (injection-safe)
def ask(text, buttons, default):
    """Dialog with buttons. Returns the button pressed, or None if closed or timed out."""
    script = ["on run argv",
              "set btns to {}",
              "repeat with i from 3 to count of argv",
              "set end of btns to item i of argv",
              "end repeat",
              "activate",
              f"set r to display dialog (item 1 of argv) with title (item 2 of argv) buttons btns "
              f"default button \"{default}\" giving up after {DIALOG_TIMEOUT}",
              "if gave up of r then return \"\"",
              "return button returned of r",
              "end run"]
    args = ["osascript"] + [a for line in script for a in ("-e", line)] + ["--", text, TITLE, *buttons]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return None
    answer = out.stdout.strip()
    return answer if answer in buttons else None


def notify(text):
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
                    "--", text, TITLE], check=False, timeout=15)


# ---------------------------------------------------------------- disks
def volume_info(mount):
    """{uuid, fs, internal, name} of the volume mounted at `mount` (diskutil), or None."""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", mount], capture_output=True, timeout=20).stdout
        info = plistlib.loads(out)
    except Exception:
        return None
    node = info.get("DeviceNode") or ""
    try:
        attached = int(os.stat(node).st_ctime)   # created when the disk is connected
    except OSError:
        attached = 0
    uuid = info.get("VolumeUUID") or info.get("DiskUUID") or info.get("DeviceIdentifier")
    return {"uuid": uuid, "connection": f"{uuid}@{attached}",
            "fs": str(info.get("FilesystemType") or info.get("FilesystemName") or "").lower(),
            "internal": bool(info.get("Internal")) and not info.get("RemovableMediaOrExternalDevice"),
            "name": info.get("VolumeName") or os.path.basename(mount), "mount": mount}


def mounted_volumes():
    vols = []
    for name in sorted(os.listdir("/Volumes")):
        path = os.path.join("/Volumes", name)
        if os.path.ismount(path) and not os.path.samefile(path, "/"):
            info = volume_info(path)
            if info and info["uuid"]:
                vols.append(info)
    return vols


def looks_like_player(vol):
    if vol["internal"] or not any(fs in vol["fs"] for fs in PLAYER_FS):
        return False
    try:
        files = os.listdir(vol["mount"])
    except PermissionError:
        raise                                  # the user is told how to grant permission
    except OSError:
        return False
    return sum(f.lower().endswith((".mp3", ".m3u")) for f in files) >= MIN_MEDIA_FILES


# ---------------------------------------------------------------- sync
def describe(plan):
    songs_in = sum(f.lower().endswith(".mp3") for f in plan["copy"])
    lists_in = sum(f.lower().endswith(".m3u") for f in plan["copy"])
    songs_out = sum(f.lower().endswith(".mp3") for f in plan["remove"])
    lists_out = sum(f.lower().endswith(".m3u") for f in plan["remove"])
    parts = []
    if songs_in or lists_in:
        parts.append(f"añadir o actualizar {songs_in} canciones y {lists_in} playlists")
    if songs_out or lists_out:
        parts.append(f"quitar {songs_out} canciones y {lists_out} playlists")
    return " y ".join(parts)


def plan_for(vol, force=False):
    """(export, plan) with "MP3 Player Card" up to date; raises ExportRefused if it is not safe."""
    from mp3_export import export_lock
    with export_lock():
        return _replan(vol, force)


def confirm_force(vol, e):
    """The export has far fewer songs than the card: second explicit confirmation."""
    answer = ask(f"El MP3 '{vol['name']}' tiene muchas más canciones que tu biblioteca, así que por seguridad "
                 f"no se ha tocado nada.\n\nSi has quitado muchas canciones a propósito (limpieza, playlists que "
                 f"ya no descargas), puedes sincronizar igualmente: se quitarán {e.remove} ficheros del MP3.",
                 ["No sincronizar", "Sincronizar igualmente"], "No sincronizar")
    return answer == "Sincronizar igualmente"


def set_cache_pending(vol, pending):
    """Records (per player) that its M3U.LIB playlist cache is pending deletion."""
    state = load_json(STATE_PATH, {})
    marks = state.setdefault("cache_pending", {})
    if pending:
        marks[vol["uuid"]] = datetime.now().isoformat(timespec="seconds")
    else:
        marks.pop(vol["uuid"], None)
    save_json(STATE_PATH, state)


def cache_is_stale(vol):
    """
    M3U.LIB to delete even if everything is up to date: only if a sync failed before deleting it (flag
    per player). Dates cannot decide it: the .m3u files keep the Mac's date and the player may have
    no clock.
    """
    return vol["uuid"] in load_json(STATE_PATH, {}).get("cache_pending", {})


def sync_device(vol, ask_first=True):
    from mp3_export import ExportRefused, Mp3Export, export_lock
    force = False
    try:
        export, plan = plan_for(vol)
    except ExportRefused as e:
        log(f"{vol['name']}: no se sincroniza sin confirmar: {e}")
        if not (e.can_force and confirm_force(vol, e)):
            if not e.can_force:
                ask(f"No se sincroniza el MP3 '{vol['name']}':\n\n{e}", ["Vale"], "Vale")
            return
        force, ask_first = True, False         # you already confirmed it with the numbers
        export, plan = plan_for(vol, force=True)
        if len(plan["remove"]) > e.remove:
            ask(f"La biblioteca ha cambiado mientras decidías. Vuelve a conectar el MP3 '{vol['name']}' "
                "para ver los cambios nuevos.", ["Vale"], "Vale")
            return
    changes = describe(plan)
    if not changes:
        if not plan.get("player_cache"):
            set_cache_pending(vol, False)   # already gone (deleted some other way): nothing pending
        elif cache_is_stale(vol):
            with export_lock():
                try:
                    Mp3Export._drop_player_cache(vol["mount"])
                    set_cache_pending(vol, False)
                    log(f"{vol['name']}: al día; borrada su caché de playlists antigua (M3U.LIB)")
                    notify(f"MP3 '{vol['name']}' al día. Al encenderlo volverá a leer las playlists")
                except OSError as e:
                    log(f"{vol['name']}: no se pudo borrar M3U.LIB: {e}")
            return
        log(f"{vol['name']}: ya está al día")
        notify(f"El MP3 '{vol['name']}' ya está al día")
        return
    # The question, without the lock: while you decide, the runner can carry on
    if ask_first:
        answer = ask(f"Se ha conectado el MP3 '{vol['name']}'.\n\nSincronizar va a {changes}.\n\n¿Sincronizar ahora?",
                     ["Ahora no", "Sincronizar"], "Sincronizar")
        if answer != "Sincronizar":
            log(f"{vol['name']}: sincronización rechazada ({answer or 'sin respuesta'})")
            return
    if not os.path.ismount(vol["mount"]):
        log(f"{vol['name']}: desconectado antes de sincronizar")
        return
    with export_lock():
        # Recompute: the library or the card may have changed in the meantime
        shown = len(plan["remove"])
        try:
            export, plan = _replan(vol, force)
        except ExportRefused as e:
            ask(f"No se sincroniza el MP3 '{vol['name']}':\n\n{e}", ["Vale"], "Vale")
            return
        if len(plan["remove"]) > shown:
            # It would now remove more than you accepted: do not go on without asking again
            ask(f"La biblioteca ha cambiado mientras decidías. Vuelve a conectar el MP3 '{vol['name']}' "
                "para ver los cambios nuevos.", ["Vale"], "Vale")
            return
        changes = describe(plan) or changes
        notify(f"Sincronizando el MP3 '{vol['name']}'…")
        log(f"{vol['name']}: sincronizando ({changes})")
        set_cache_pending(vol, True)   # if it fails halfway, the next connection deletes M3U.LIB even if up to date
        try:
            export.sync_card(vol["mount"], plan=plan)
            set_cache_pending(vol, False)
        except OSError as e:
            log(f"{vol['name']}: error al copiar: {e}")
            ask(f"Error al sincronizar el MP3 '{vol['name']}':\n\n{e}\n\nPara reintentar, expúlsalo desde el "
                "Finder, desconéctalo y vuelve a conectarlo.", ["Vale"], "Vale")
            return
    if ask(f"MP3 '{vol['name']}' sincronizado: {changes}.\n\nAl encenderlo (sin el cable) volverá a leer las "
           "playlists; puede tardar un poco más de lo normal.\n\n¿Expulsarlo para desconectarlo?",
           ["Dejarlo conectado", "Expulsar"], "Expulsar") == "Expulsar":
        out = subprocess.run(["diskutil", "eject", vol["mount"]], capture_output=True, text=True)
        notify("Ya puedes desconectar el MP3" if out.returncode == 0 else f"No se pudo expulsar: {out.stderr.strip()}")


def _replan(vol, force=False):
    """Like plan_for, for when the lock is already held."""
    from mp3_export import Mp3Export
    export = Mp3Export()
    export.build()
    return export, export.plan_card(vol["mount"], force=force)


def check_volumes():
    devices = load_json(DEVICES_PATH, {})
    known, ignored = devices.setdefault("players", {}), devices.setdefault("ignored", {})
    state = load_json(STATE_PATH, {})
    seen = set(state.get("seen", []))
    vols = mounted_volumes()
    state["seen"] = [v["connection"] for v in vols]   # current connections only
    save_json(STATE_PATH, state)                 # before asking: one connection = one question
    for vol in vols:
        if vol["connection"] in seen or vol["uuid"] in ignored:
            continue
        try:
            handle_volume(vol, devices)
        except PermissionError:
            log(f"{vol['name']}: sin permiso para leerlo\n{traceback.format_exc()}")
            ask(PERMISSION_HELP, ["Vale"], "Vale")
        except Exception as e:
            # Any other failure: report it and forget this connection to retry with the next disk
            log(f"{vol['name']}: error\n{traceback.format_exc()}")
            state = load_json(STATE_PATH, {})   # re-read: sync_device may have changed its flags
            state["seen"] = [c for c in state.get("seen", []) if c != vol["connection"]]
            save_json(STATE_PATH, state)
            ask(f"Error al revisar el disco '{vol['name']}':\n\n{e}\n\nDetalles en logs/mp3sync.log.", ["Vale"], "Vale")


def handle_volume(vol, devices):
    known, ignored = devices["players"], devices["ignored"]
    if vol["uuid"] in known:
        sync_device(vol)
    elif looks_like_player(vol):
        answer = ask(f"Se ha conectado '{vol['name']}'. ¿Es tu reproductor MP3?\n\nSi lo es, se sincronizará con "
                     "tu biblioteca cada vez que lo conectes (siempre preguntando antes).",
                     ["No es mi MP3", "Ahora no", "Sí, es mi MP3"], "Sí, es mi MP3")
        if answer == "Sí, es mi MP3":
            known[vol["uuid"]] = {"name": vol["name"], "added": datetime.now().isoformat(timespec="seconds")}
            save_json(DEVICES_PATH, devices)
            sync_device(vol)
        elif answer == "No es mi MP3":
            ignored[vol["uuid"]] = {"name": vol["name"]}
            save_json(DEVICES_PATH, devices)


# ---------------------------------------------------------------- LaunchAgent
def install():
    rc, err = install_launch_agent(LABEL, __file__, "mp3sync.log", StartOnMount=True, RunAtLoad=False)
    print("LaunchAgent instalado: el Mac preguntará al conectar el MP3." if rc == 0
          else f"No se pudo activar: {err}")
    return rc


def uninstall():
    uninstall_launch_agent(LABEL)
    print("LaunchAgent quitado.")
    return 0


def main(args):
    if args == ["--install"]:
        return install()
    if args == ["--uninstall"]:
        return uninstall()
    if args == ["--forget"]:
        save_json(DEVICES_PATH, {})
        print("Reproductores olvidados: se volverá a preguntar al conectarlos.")
        return 0
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    # If a check is already running (e.g. waiting for your answer), this one waits and then looks only
    # at the disks still unchecked
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        check_volumes()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    os.chdir(HERE)
    sys.exit(main(sys.argv[1:]))
