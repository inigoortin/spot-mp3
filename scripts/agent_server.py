"""
Mac agent for the Mi Música phone app (home Wi-Fi only).

- HTTP + WebSocket API on port 8765. Only local-network addresses are accepted and, except /ping
  and pairing, every call needs the token the phone receives when pairing.
- Pairing: the phone asks for a code (/pair/start), the Mac shows it in a notification, the phone
  sends it back (/pair/finish) and gets its token. Limits and blocks: pairing_guard.py.
- Jobs (/jobs): a persistent queue run one at a time with the usual scripts (runner.sh,
  playlist_builder.py, spotify_upload.py). Each job has a client_id from the phone, so a resent
  job never runs twice. Phone edits (edit_playlist, delete_playlist) are jobs too, so they work
  offline: the phone queues them and they are applied as soon as they arrive.
- Live progress over WebSocket (/ws); playlists, covers, versions to review.
- Advertised on the Wi-Fi (_spotmp3._tcp) so the phone finds it by itself.
- The Mac sleeps normally; it only stays awake while a job runs (caffeinate tied to the job).

  python agent_server.py               run the agent
  python agent_server.py --install     install it as a LaunchAgent (starts at login)
  python agent_server.py --uninstall   remove the LaunchAgent
  python agent_server.py --unblock [ip]   lift pairing blocks
"""

import asyncio
import hashlib
import io
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
import requests
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from mutagen.id3 import ID3
from PIL import Image
from pydantic import BaseModel
from zeroconf import ServiceInfo, Zeroconf

import playlist_store as ps
from common import CACHE_DIR, PRIVATE_DIR, SCRIPTS_DIR, ROOT, DATA_DIR, install_launch_agent, load_json, save_json, uninstall_launch_agent
from mp3_export import Mp3Export, export_lock
from version_review import APPROVED, REJECTED, ReviewStore
from pairing_guard import PairingGuard, PairingRefused

VERSION = "2.0"
PORT = 8765
LABEL = "com.spotmp3.agent"
HERE = SCRIPTS_DIR
PY = sys.executable
TOKENS_PATH = os.path.join(PRIVATE_DIR, ".agent_tokens.json")
JOBS_PATH = os.path.join(DATA_DIR, "agent_jobs.json")
COVER_DIR = os.path.join(CACHE_DIR, ".cover_cache")
IMAGE_URLS_PATH = os.path.join(CACHE_DIR, "spotify_images.json")
LOGS_DIR = os.path.join(ROOT, "logs")
MAX_JOBS_KEPT = 200
MAX_LOG_LINES = 400
PAIR_BLOCKS_PATH = os.path.join(PRIVATE_DIR, ".agent_pair_blocks.json")
DEVICE_MAX_LEN = 40
JOB_TYPES = {"refresh", "build_playlist", "publish", "rebuild_playlists", "edit_playlist", "delete_playlist",
             "review_version", "spotify_playlist"}
INSTANT_JOBS = {"edit_playlist", "delete_playlist", "review_version", "spotify_playlist"}   # applied on arrival, without going through the queue

@asynccontextmanager
async def lifespan(_app):
    await startup()   # defined below, next to the worker and the Wi-Fi advertising it starts
    yield


app = FastAPI(title="spot-mp3 agent", version=VERSION, lifespan=lifespan)
_lock = threading.RLock()
_loop = None            # uvicorn's asyncio loop (to notify WebSockets from the worker thread)
_sockets = set()


AGENT_LOG = os.path.join(LOGS_DIR, "agent.log")   # opened by launchd (StandardOutPath, in append mode)
AGENT_LOG_MAX = 2_000_000


def rotate_log(path=AGENT_LOG, limit=AGENT_LOG_MAX):
    """
    If the log exceeds `limit` bytes, it is copied to agent.log.1 and emptied. Emptied rather than
    renamed because launchd keeps it open: once renamed, it would keep writing to the old one.
    """
    try:
        if os.path.getsize(path) > limit:
            shutil.copyfile(path, path + ".1")
            os.truncate(path, 0)
    except OSError:
        pass


def log(msg):
    rotate_log()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- security
def _is_local_ip(text):
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _host_allowed(host_header):
    """
    The Host header must be a local IP or this Mac's own name. So a web page that points its domain at
    this IP (DNS rebinding) cannot call the agent from the browser of someone on the network.
    """
    host = host_header.strip().lower()
    if host.startswith("["):                      # [::1]:8765
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                     # 192.168.1.5:8765 / nombre:8765
        host = host.split(":", 1)[0]
    host = host.rstrip(".")                        # "mac.local." == "mac.local"
    if _is_local_ip(host):
        return True
    name = socket.gethostname().lower().rstrip(".")
    short = name.split(".")[0]
    return host in {"localhost", name, short, f"{short}.local"}


@app.middleware("http")
async def local_network_only(request: Request, call_next):
    """Home network only: private or local source address and a Host that is this Mac."""
    client = request.client.host if request.client else ""
    if not _is_local_ip(client) or not _host_allowed(request.headers.get("host", "")):
        return JSONResponse({"detail": "solo red local"}, status_code=403)
    return await call_next(request)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(token):
    return bool(token) and _hash(token) in load_json(TOKENS_PATH, {}, strict=True)


def require_token(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not check_token(token):
        raise HTTPException(401, "no emparejado")
    return token


def notify_mac(title, text):
    """
    Mac notification (to show the pairing code). Texts are passed as script arguments, never inside
    the AppleScript code: whatever the phone sends cannot be executed.
    """
    try:
        subprocess.run(["osascript",
                        "-e", "on run argv",
                        "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                        "-e", "end run",
                        "--", title, text], check=False, timeout=10)
    except subprocess.TimeoutExpired:
        log(f"la notificación no se pudo mostrar a tiempo: {title}")


def clean_device(name):
    """Device name safe to show and log: printable, no quotes, short."""
    text = "".join(c for c in str(name) if c.isprintable() and c not in "\\\"'`")
    return " ".join(text.split())[:DEVICE_MAX_LEN] or "Android"


class PairStart(BaseModel):
    device: str = "Android"


class PairFinish(BaseModel):
    pairing_id: str
    code: str


@app.get("/ping")
def ping():
    return {"app": "spot-mp3-agent", "version": VERSION, "host": socket.gethostname()}


@app.delete("/pair")
def unpair(token=Depends(require_token)):
    """The phone forgets the pairing: its token stops working on the Mac too."""
    with _lock:
        tokens = load_json(TOKENS_PATH, {}, strict=True)
        device = tokens.pop(_hash(token), {}).get("device")
        save_json(TOKENS_PATH, tokens, private=True)
    log(f"dispositivo desemparejado: {device!r}")
    return {"unpaired": True}


_guard = None   # PairingGuard; created below, once log() and notify_mac() exist


def guard():
    global _guard
    with _lock:   # two requests right after start-up must not create two
        if _guard is None:
            _guard = PairingGuard(PAIR_BLOCKS_PATH, alert=lambda text: notify_mac("Mi Música · seguridad", text),
                                  log=log)
        return _guard


@app.post("/pair/start")
def pair_start(body: PairStart, request: Request):
    ip = request.client.host if request.client else ""
    device = clean_device(body.device)
    try:
        pairing_id, code = guard().start(ip, device)
    except PairingRefused as e:
        raise HTTPException(e.status, e.message)
    notify_mac("Mi Música", f"Código para {device}: {code}")
    log(f"emparejado solicitado por {device!r} desde {ip}; código mostrado en notificación")
    return {"pairing_id": pairing_id, "expires_in": PairingGuard.CODE_TTL}


@app.post("/pair/finish")
def pair_finish(body: PairFinish, request: Request):
    ip = request.client.host if request.client else ""
    try:
        device = guard().finish(ip, body.pairing_id, body.code)
    except PairingRefused as e:
        raise HTTPException(e.status, e.message)
    token = secrets.token_urlsafe(32)
    with _lock:
        tokens = load_json(TOKENS_PATH, {}, strict=True)
        tokens[_hash(token)] = {"device": device, "created": ps.now()}
        save_json(TOKENS_PATH, tokens, private=True)
    log(f"dispositivo emparejado: {device!r} desde {ip}")
    return {"token": token, "host": socket.gethostname()}


# ---------------------------------------------------------------- jobs (persistent queue)
class JobIn(BaseModel):
    client_id: str
    type: str
    params: dict = {}


def jobs_load():
    return load_json(JOBS_PATH, [], strict=True)


def jobs_save(jobs):
    """Keeps the last MAX_JOBS_KEPT finished jobs and ALL pending or running ones."""
    finished = [j for j in jobs if j["status"] not in ("queued", "running")]
    keep_ids = {id(j) for j in finished[-MAX_JOBS_KEPT:]}
    save_json(JOBS_PATH, [j for j in jobs if j["status"] in ("queued", "running") or id(j) in keep_ids])


def tail_lines(path, n=3, chunk=8192):
    """Last n non-empty lines, reading only the end of the file (logs can be huge)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - chunk))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    return [l.strip() for l in lines if l.strip()][-n:]


def job_public(job, with_log=False):
    out = {k: v for k, v in job.items() if k != "log"}
    out["log_tail"] = job.get("log", [])[-(MAX_LOG_LINES if with_log else 6):]
    return out


def broadcast(event):
    if _loop is None:
        return
    async def send():
        for ws in list(_sockets):
            try:
                await ws.send_json(event)
            except Exception:
                _sockets.discard(ws)
    asyncio.run_coroutine_threadsafe(send(), _loop)


def update_job(job_id, **fields):
    with _lock:
        jobs = jobs_load()
        for job in jobs:
            if job["id"] == job_id:
                lines = fields.pop("append_log", None)
                job.update(fields)
                if lines:
                    job["log"] = (job.get("log", []) + lines)[-MAX_LOG_LINES:]
                jobs_save(jobs)
                broadcast({"event": "job", "job": job_public(job), "new_lines": lines or []})
                return job
    return None


# Playlist orderings offered by the phone form ("" = by similarity)
ORDER_BY = ["", "bpm_exact", "-bpm_exact", "-energy", "energy", "-valence", "valence", "-acousticness", "camelot"]


def validate_job(body):
    p = body.params
    if body.type not in JOB_TYPES:
        raise HTTPException(400, f"tipo desconocido: {body.type}")
    if body.type == "review_version":
        if not str(p.get("track_id", "")).isalnum() or p.get("decision") not in ("approve", "reject"):
            raise HTTPException(400, "track_id y decision (approve | reject) son obligatorios")
        return
    if body.type not in ("refresh", "rebuild_playlists") and not str(p.get("name", "")).strip():
        raise HTTPException(400, "falta el nombre de la playlist")
    if body.type == "spotify_playlist":
        if not (isinstance(p.get("id"), str) and p["id"].isalnum() and isinstance(p.get("download"), bool)):
            raise HTTPException(400, "spotify_playlist necesita id (de Spotify) y download (true/false)")
        return
    if body.type == "build_playlist" and not isinstance(p.get("criteria", {}), dict):
        raise HTTPException(400, "criteria debe ser un objeto")
    if body.type == "build_playlist":
        problem = ps.name_problem(p["name"])
        if problem:
            raise HTTPException(409, f"No se puede crear: {problem}")
        import playlist_builder
        problems = playlist_builder.criteria_problems(p.get("criteria", {}), ps.songs(), p.get("limit", 50))
        if p.get("order_by") not in ORDER_BY + [None]:
            problems.append(f"orden desconocido: {p.get('order_by')!r}")
        if problems:
            raise HTTPException(400, "Criterios no válidos: " + "; ".join(problems))
    if body.type == "edit_playlist" and not all(isinstance(p.get(k, []), list) and all(isinstance(x, str) for x in p.get(k, []))
                                                for k in ("add", "remove", "restore")):
        raise HTTPException(400, "add, remove y restore deben ser listas de ids")
    if body.type == "publish" and not ps.exists(p["name"]):
        raise HTTPException(404, "no existe esa playlist")


@app.post("/jobs")
def create_job(body: JobIn, _=Depends(require_token)):
    with _lock:
        jobs = jobs_load()
        existing = next((j for j in jobs if j["client_id"] == body.client_id), None)
        if existing:
            if existing["type"] in INSTANT_JOBS and existing["status"] == "running":
                # The first copy is still being applied (and could fail and be withdrawn): let it retry
                raise HTTPException(503, "esta orden se está aplicando; se reintentará")
            return job_public(existing)  # phone retry: the job is already here (before validating again)
        validate_job(body)
        job = {"id": uuid.uuid4().hex[:12], "client_id": body.client_id, "type": body.type, "params": body.params,
               "status": "running" if body.type in INSTANT_JOBS else "queued", "created": ps.now(),
               "started": None, "finished": None, "result": None, "log": []}
        # Recorded right away (instant ones too, as "running"): a simultaneous resend of the same job finds
        # it and does not apply it again (matters for "Undo": a late repeated "remove" would remove the
        # song again)
        jobs.append(job)
        jobs_save(jobs)
    if body.type in INSTANT_JOBS:
        # Outside _lock: waits for the playlists lock without holding up the rest of the agent
        try:
            outcome = run_instant(job)
        except Exception as e:
            # Unexpected error (e.g. the CSV was being rewritten): the job is withdrawn and 503 returned, so
            # the phone retries it instead of marking it failed forever
            log(f"edición del móvil no aplicada, se reintentará: {e}")
            with _lock:
                jobs = [j for j in jobs_load() if j["id"] != job["id"]]
                save_json(JOBS_PATH, jobs)
            raise HTTPException(503, f"no se pudo aplicar ahora ({e}); se reintentará")
        lines = outcome.pop("log", [])
        job = update_job(job["id"], **outcome, started=job["created"], finished=ps.now(), append_log=lines) or job
    log(f"orden recibida: {body.type} {body.params} -> {job['status']}")
    broadcast({"event": "job", "job": job_public(job), "new_lines": []})
    if body.type == "review_version":
        broadcast({"event": "reviews_changed"})
        broadcast({"event": "playlists_changed"})
    elif body.type in INSTANT_JOBS:
        broadcast({"event": "playlists_changed", "name": body.params.get("name")})
        refresh_mp3_export()
    else:
        _wake_worker.set()
    return job_public(job)


_export_pending = threading.Event()


def refresh_mp3_export():
    """
    Updates "MP3 Player Card" after a playlist change made from the phone, in the background (does not
    delay the response). Several requests in a row are merged into one export.
    """
    if _export_pending.is_set():
        return
    _export_pending.set()

    def run():
        time.sleep(2)
        _export_pending.clear()
        try:
            with export_lock():   # not at the same time as the runner or the card sync
                Mp3Export().build()
        except Exception as e:
            log(f"MP3 Player Card no actualizada: {e}")

    threading.Thread(target=run, daemon=True).start()


REVIEWS = ReviewStore()


def run_review(p):
    """Approve or remove a reviewed version from the phone (repeating it changes nothing)."""
    tid, decision = p["track_id"], p["decision"]
    entry = REVIEWS.load().get(tid)
    if not entry:
        return {"status": "done", "result": {"returncode": 0, "missing": True}, "log": [f"{tid}: no está para revisar"]}
    target = APPROVED if decision == "approve" else REJECTED
    if target == REJECTED and not (entry.get("status") == REJECTED and entry.get("restored")):
        try:
            entry = REVIEWS.reject(tid)   # repeatable: a removal that failed halfway gets completed
        except RuntimeError as e:
            return {"status": "failed", "result": {"returncode": 1, "error": str(e)}, "log": [str(e)]}
    elif target == APPROVED and entry.get("status") == REJECTED:
        return {"status": "done", "result": {"returncode": 0, "song": entry["song"], "decision": "reject"},
                "log": [f"{entry['song']}: ya estaba quitada; se deja así"]}
    elif target == APPROVED and entry.get("status") != APPROVED:
        entry = REVIEWS.approve(tid)
    verb = ("aprobada" if target == APPROVED else
            "quitada; vuelve el MP3 anterior" if entry.get("restored") else "quitada del MP3 y de las playlists")
    return {"status": "done", "result": {"returncode": 0, "song": entry["song"], "decision": decision},
            "log": [f"{entry['song']}: {verb}"]}


def run_instant(job):
    """Phone edits. Repeating them changes nothing, so a resend is harmless."""
    if job["type"] == "review_version":
        return run_review(job["params"])
    if job["type"] == "spotify_playlist":
        return run_spotify_playlist(job)
    p, name = job["params"], job["params"]["name"]
    if not ps.exists(name):
        # Already deleted (e.g. from the Mac): for the phone the job is done
        return {"status": "done", "result": {"returncode": 0, "playlist": name, "count": 0, "missing": True},
                "log": [f"'{name}' ya no existe"]}
    if job["type"] == "delete_playlist":
        entry = ps.entry(name)
        if entry.get("source") == "spotify" and entry.get("spotify_id"):
            # Deleting a playlist downloaded from Spotify on the phone = stop downloading it (before deleting:
            # otherwise a simultaneous rebuild could recreate it)
            import spotify_playlists as spl
            spl.set_download(entry["spotify_id"], None, False)
        ps.delete(name)
        return {"status": "done", "result": {"returncode": 0, "playlist": name, "deleted": True},
                "log": [f"'{name}' movida a Playlists/.papelera"]}
    try:
        res = ps.edit(name, add=p.get("add", []), remove=p.get("remove", []), restore=p.get("restore", []))
    except KeyError:
        # Deleted at the same moment (from the Mac or another phone): for the phone the job is done
        return {"status": "done", "result": {"returncode": 0, "playlist": name, "count": 0, "missing": True},
                "log": [f"'{name}' ya no existe"]}
    added = len(p.get("add", [])) + len(p.get("restore", [])) - len(res["rejected"])
    return {"status": "done", "result": {"returncode": 0, "playlist": name, **res},
            "log": [f"'{name}': -{len(p.get('remove', []))} +{added}"]}


def run_spotify_playlist(job):
    """
    Choose (download=true) or drop (false) a Spotify playlist for download. Choosing one queues a
    library update (export + downloads + playlists) if there is none already.
    """
    import spotify_playlists as spl
    p = job["params"]
    if p["download"]:
        spl.set_download(p["id"], p["name"], True)
        queued = enqueue_refresh(f"{job['client_id']}:refresh")
        return {"status": "done", "result": {"returncode": 0, "playlist": p["name"], "download": True,
                                             "refresh_queued": queued},
                "log": [f"'{p['name']}' se descargará" + (" (actualización en cola)" if queued else "")]}
    local = (spl.load().get(p["id"]) or {}).get("local_name")
    spl.set_download(p["id"], None, False)
    if local and ps.exists(local) and ps.entry(local).get("spotify_id") == p["id"]:
        ps.delete(local)
    return {"status": "done", "result": {"returncode": 0, "playlist": p["name"], "download": False},
            "log": [f"'{p['name']}' ya no se descarga; sus canciones se quitan en la próxima limpieza "
                    "si no están en otra lista"]}


def enqueue_refresh(client_id):
    """Queues a library update unless one is already pending. Returns whether it queued one."""
    with _lock:
        jobs = jobs_load()
        if any(j["type"] == "refresh" and j["status"] == "queued" for j in jobs) or \
                any(j["client_id"] == client_id for j in jobs):
            return False
        jobs.append({"id": uuid.uuid4().hex[:12], "client_id": client_id, "type": "refresh", "params": {},
                     "status": "queued", "created": ps.now(), "started": None, "finished": None, "result": None,
                     "log": []})
        jobs_save(jobs)
    _wake_worker.set()
    return True


@app.get("/spotify/playlists")
def spotify_playlists_list(_=Depends(require_token)):
    """Your Spotify playlists (own and followed) and which ones are downloaded."""
    import spotify_playlists as spl
    try:
        return spl.remote_playlists(spotify_full())
    except SystemExit as e:
        if e.code == 2:
            raise HTTPException(503, "Spotify necesita iniciar sesión en el Mac (spotify_export.py a mano)")
        raise HTTPException(502, "No se pudo conectar con Spotify desde el Mac (¿sin internet?)")
    except Exception as e:
        raise HTTPException(502, f"Spotify no responde ({e})")


_spotify_full = None


def spotify_full():
    """Spotify client allowed to read your playlists (same saved login)."""
    global _spotify_full
    if _spotify_full is None:
        from spotify_auth import SCOPES_PLAYLISTS, SCOPES_READ, get_client
        _spotify_full = get_client(f"{SCOPES_READ} {SCOPES_PLAYLISTS}", "spotify_export.py")
    return _spotify_full


@app.get("/jobs")
def list_jobs(limit: int = 30, _=Depends(require_token)):
    return [job_public(j) for j in jobs_load()[-limit:]][::-1]


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _=Depends(require_token)):
    """Cancels a job the Mac has not started yet (queued). Running or finished: 409."""
    with _lock:
        jobs = jobs_load()
        job = next((j for j in jobs if j["id"] == job_id or j["client_id"] == job_id), None)
        if not job:
            raise HTTPException(404, "no existe")
        if job["status"] == "cancelled":
            return job_public(job)   # cancelling twice changes nothing
        if job["status"] != "queued":
            raise HTTPException(409, "ya se está ejecutando en el Mac; no se puede cancelar" if job["status"] == "running"
                                else "el Mac ya la ha terminado; no se puede cancelar")
        job.update(status="cancelled", finished=ps.now(), result={"returncode": 0, "cancelled": True})
        job["log"] = job.get("log", []) + ["(cancelada desde el móvil)"]
        jobs_save(jobs)
    log(f"orden cancelada desde el móvil: {job['type']} ({job['id']})")
    broadcast({"event": "job", "job": job_public(job), "new_lines": []})
    return job_public(job)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, _=Depends(require_token)):
    job = next((j for j in jobs_load() if j["id"] == job_id or j["client_id"] == job_id), None)
    if not job:
        raise HTTPException(404, "no existe")
    return job_public(job, with_log=True)


def command_for(job):
    p = job["params"]
    if job["type"] == "refresh":
        return ["/bin/bash", os.path.join(SCRIPTS_DIR, "runner.sh")]
    if job["type"] == "rebuild_playlists":
        return [PY, "playlist_builder.py", "--all"]
    if job["type"] == "build_playlist":
        cmd = [PY, "playlist_builder.py", "--create", p["name"], "--criteria", json.dumps(p.get("criteria", {})),
               "--limit", str(int(p.get("limit") or 50))]
        if p.get("order_by"):
            cmd += ["--order-by", p["order_by"]]
        return cmd
    if job["type"] == "publish":
        return [PY, "spotify_upload.py", p["name"]]
    raise ValueError(job["type"])


def result_for(job, returncode, lines):
    p = job["params"]
    result = {"returncode": returncode}
    if job["type"] in ("build_playlist", "publish") and ps.exists(p["name"]):
        result.update(playlist=p["name"], count=len(ps.read_tracks(p["name"])),
                      spotify_url=ps.entry(p["name"]).get("spotify_url"))
    if job["type"] == "refresh":
        result["summary"] = [l for l in lines if l.startswith(("Descargadas", "Se van a descargar", "No hay canciones",
                                                                "Limpieza", "== ", "!!", "Aviso"))][-10:]
    if returncode == 2:
        result["error"] = "Hace falta iniciar sesión en Spotify en el Mac (ejecuta el script a mano una vez)."
    return result


def claim_job(job_id):
    """
    Moves the job from "queued" to "running" in one step; None if it is no longer queued (e.g. cancelled).
    """
    with _lock:
        jobs = jobs_load()
        job = next((j for j in jobs if j["id"] == job_id), None)
        if not job or job["status"] != "queued":
            return None
        job.update(status="running", started=ps.now())
        jobs_save(jobs)
    broadcast({"event": "job", "job": job_public(job), "new_lines": []})
    return job


def run_job(job):
    if claim_job(job["id"]) is None:
        log(f"{job['type']} ({job['id']}) ya no está en cola (cancelada); no se ejecuta")
        return
    log(f"ejecutando {job['type']} ({job['id']})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    lines, buffer, last_flush = [], [], time.time()
    try:
        proc = subprocess.Popen(command_for(job), cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env, bufsize=1)
        keep_awake_while(proc.pid)
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line or "tensorflow" in line or line.startswith(("I0000", "WARNING: All log", "[ WARNING")):
                continue
            lines.append(line)
            buffer.append(line)
            if time.time() - last_flush > 0.7:
                update_job(job["id"], append_log=buffer)
                buffer, last_flush = [], time.time()
        returncode = proc.wait()
    except Exception as e:
        buffer.append(f"error: {e}")
        returncode = -1
    status = "done" if returncode == 0 else "failed"
    update_job(job["id"], append_log=buffer, status=status, finished=ps.now(), result=result_for(job, returncode, lines))
    if job["type"] in ("build_playlist", "publish", "refresh", "rebuild_playlists"):
        broadcast({"event": "playlists_changed"})
    if job["type"] in ("build_playlist", "rebuild_playlists"):   # refresh: runner.sh already does it
        refresh_mp3_export()
    log(f"{job['type']} ({job['id']}) -> {status}")


_wake_worker = threading.Event()


def worker():
    # If the agent restarted in the middle of a job, it is queued again (every job is repeatable)
    # (instant edits left halfway are applied again right here)
    with _lock:
        jobs = jobs_load()
        unfinished_instant = []
        for j in jobs:
            if j["status"] == "running":
                j["log"] = j.get("log", []) + ["(el agente se reinició; se repite la orden)"]
                if j["type"] in INSTANT_JOBS:
                    unfinished_instant.append(j)
                else:
                    j["status"] = "queued"
        jobs_save(jobs)
    for j in unfinished_instant:
        try:
            outcome = run_instant(j)
        except Exception as e:
            outcome = {"status": "failed", "result": {"returncode": -1, "error": str(e)}, "log": [f"error: {e}"]}
        lines = outcome.pop("log", [])
        update_job(j["id"], **outcome, finished=ps.now(), append_log=lines)
    while True:
        job = next((j for j in jobs_load() if j["status"] == "queued"), None)
        if job:
            run_job(job)
            continue
        _wake_worker.wait(timeout=30)
        _wake_worker.clear()


# ---------------------------------------------------------------- status and library data
@app.get("/status")
def status(_=Depends(require_token)):
    jobs = jobs_load()
    runs = sorted(f for f in os.listdir(LOGS_DIR) if f.startswith("runner_")) if os.path.isdir(LOGS_DIR) else []
    last_run = None
    if runs:
        tail = tail_lines(os.path.join(LOGS_DIR, runs[-1]))
        last_run = {"file": runs[-1], "when": datetime.fromtimestamp(os.path.getmtime(os.path.join(LOGS_DIR, runs[-1]))).isoformat(timespec="seconds"),
                    "last_line": tail[-1] if tail else ""}
    return {"version": VERSION, "host": socket.gethostname(),
            "running": next((job_public(j) for j in jobs if j["status"] == "running"), None),
            "queued": sum(j["status"] == "queued" for j in jobs),
            "songs": len(ps.songs()), "playlists": len(ps.all_names()), "last_refresh": last_run,
            "pending_reviews": len(safe_pending())}


def safe_pending():
    """Pending reviews; if version_review.json is corrupt, it is logged and nothing else breaks."""
    try:
        return REVIEWS.pending()
    except ValueError as e:
        log(f"version_review.json dañado: {e}")
        return []


@app.get("/reviews")
def list_reviews(_=Depends(require_token)):
    """Versions pending review (different from Spotify's)."""
    return [{"track_id": r["track_id"], "song": r.get("song"), "reason": r.get("reason"), "video": r.get("video"),
             "added": r.get("added"), "playable": bool(r.get("file")) and os.path.exists(r["file"]),
             "has_previous": bool(r.get("previous"))}
            for r in safe_pending()]


@app.get("/audio/{track_id}")
def audio(track_id: str, token: str = "", authorization: str = Header(default="")):
    """A song's MP3, to listen to it on the phone while reviewing (token as in /cover)."""
    if not (check_token(token) or check_token(authorization.removeprefix("Bearer ").strip())):
        raise HTTPException(401, "no emparejado")
    if not track_id.isalnum():
        raise HTTPException(400, "id no válido")
    path = (REVIEWS.load().get(track_id) or {}).get("file")
    if not path or not os.path.exists(path):
        df = ps.songs()
        rows = df[df["track_id"] == track_id]
        path = rows["file_path"].iloc[0] if len(rows) else None
    if not path or not os.path.exists(path) or not os.path.realpath(path).startswith(os.path.join(ROOT, "MP3 Songs") + os.sep):
        raise HTTPException(404, "sin MP3")
    return FileResponse(path, media_type="audio/mpeg")



@app.get("/meta")
def meta(_=Depends(require_token)):
    """Options for the phone forms (genres, groups, ranges, artists...)."""
    df = ps.songs()
    num = ["energy", "danceability", "valence", "acousticness", "speechiness", "bpm_exact"]
    import playlist_builder
    return {
        "genres": [{"name": g, "count": int(c)} for g, c in df["genre"].value_counts().items()],
        "genre_families": sorted(df["genre_family"].dropna().unique().tolist()),
        "groups": {c: sorted(df[c].dropna().unique().tolist()) for c in ("energy_group", "danceability_group", "valence_group", "mode")},
        "metrics": {c: {"min": float(df[c].min()), "max": float(df[c].max())} for c in num},
        "order_by": ORDER_BY,
        "artists": [{"name": a, "count": int(c)} for a, c in df["artist_name"].value_counts().head(400).items()],
        "presets": list(playlist_builder.PRESETS),
    }


def song_view(row):
    return {"track_id": row["track_id"], "artist": row["artist_name"], "title": row["track_name"],
            "genre": row["genre"], "energy": _num(row.get("energy")), "valence": _num(row.get("valence")),
            "danceability": _num(row.get("danceability")), "bpm": _num(row.get("bpm_exact")),
            "camelot": row.get("camelot") if isinstance(row.get("camelot"), str) else None, "in_library": True}


def _num(v):
    try:
        return None if pd.isna(v) else round(float(v), 1)
    except (TypeError, ValueError):
        return None


@app.get("/songs")
def search_songs(q: str = "", genre: str = "", limit: int = 50, offset: int = 0, _=Depends(require_token)):
    df = ps.songs()
    if q:
        text = (df["artist_name"].fillna("") + " " + df["track_name"].fillna("")).str.lower()
        for word in q.lower().split():
            df = df[text.loc[df.index].str.contains(word, regex=False)]
    if genre:
        df = df[df["genre"] == genre]
    df = df.drop_duplicates("track_id").sort_values(["artist_name", "track_name"])
    return {"total": len(df), "songs": [song_view(r) for _, r in df.iloc[offset:offset + limit].iterrows()]}


# ---------------------------------------------------------------- playlists
def playlist_summary(name, lib=None):
    entry = ps.entry(name)
    tracks = ps.read_tracks(name, lib)
    return {"name": name, "source": entry.get("source"), "count": len(tracks), "edited": ps.is_edited(entry),
            "excluded_count": len(entry.get("excluded") or []), "pinned_count": len(entry.get("pinned") or []),
            "spotify_url": entry.get("spotify_url"), "updated": entry.get("updated"),
            "criteria": entry.get("criteria"), "cover_ids": mosaic_ids(tracks)}


def mosaic_ids(tracks):
    """4 songs by different artists (if possible), so the mosaic does not repeat covers."""
    picked, artists = [], set()
    for t in tracks:
        if t["artist"] not in artists:
            picked.append(t["track_id"])
            artists.add(t["artist"])
        if len(picked) == 4:
            return picked
    rest = [t["track_id"] for t in tracks if t["track_id"] not in picked]
    return (picked + rest)[:4]


@app.get("/playlists")
def list_playlists(_=Depends(require_token)):
    lib = ps.songs()
    return [playlist_summary(n, lib) for n in ps.all_names()]


@app.get("/playlists/{name}")
def get_playlist(name: str, _=Depends(require_token)):
    if not ps.exists(name):
        raise HTTPException(404, "no existe")
    songs = ps.songs()
    lib = songs.drop_duplicates("track_id").set_index("track_id", drop=False)
    tracks = [song_view(lib.loc[t["track_id"]]) for t in ps.read_tracks(name, songs) if t["track_id"] in lib.index]
    # Songs removed by hand that are still in the library: the phone shows them so they can be restored
    excluded = [song_view(lib.loc[tid]) for tid in ps.entry(name).get("excluded") or [] if tid in lib.index]
    return {**playlist_summary(name, songs), "tracks": tracks, "excluded": excluded}


# (The old POST /playlists/{name}/edit and DELETE /playlists/{name} were removed: the app edits and
# deletes with edit_playlist / delete_playlist jobs, which are deduplicated and support "undo".)


# ---------------------------------------------------------------- covers
_spotify = None


def spotify():
    global _spotify
    if _spotify is None:
        from spotify_auth import SCOPES_READ, get_client
        _spotify = get_client(SCOPES_READ, "spotify_export.py")
    return _spotify


def remember_image(track_id, url):
    with _lock:
        images = load_json(IMAGE_URLS_PATH, {})
        images[track_id] = url
        save_json(IMAGE_URLS_PATH, images)



def cover_bytes(track_id):
    """Original cover: from the MP3 if it is in the library; otherwise from Spotify."""
    lib = ps.songs()
    match = lib[lib["track_id"] == track_id]
    if not match.empty and os.path.exists(match.iloc[0]["file_path"]):
        try:
            for key, frame in ID3(match.iloc[0]["file_path"]).items():
                if key.startswith("APIC"):
                    return frame.data
        except Exception:
            pass
    url = load_json(IMAGE_URLS_PATH, {}).get(track_id)
    if not url:
        try:
            images = spotify().track(track_id, market="from_token")["album"].get("images") or []
        except BaseException:
            images = []
        if not images:
            return None
        url = images[min(1, len(images) - 1)]["url"]
        remember_image(track_id, url)
    r = requests.get(url, timeout=20)
    return r.content if r.ok else None


@app.get("/cover/{track_id}")
def cover(track_id: str, size: int = 300, token: str = "", authorization: str = Header(default="")):
    # Images accept the token as a parameter (?token=) so they can be loaded directly
    if not (check_token(token) or check_token(authorization.removeprefix("Bearer ").strip())):
        raise HTTPException(401, "no emparejado")
    size = max(64, min(size, 640))
    if not track_id.isalnum():
        raise HTTPException(400, "id no válido")
    os.makedirs(COVER_DIR, exist_ok=True)
    cached = os.path.join(COVER_DIR, f"{track_id}_{size}.jpg")
    if not os.path.exists(cached):
        data = cover_bytes(track_id)
        if not data:
            raise HTTPException(404, "sin portada")
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((size, size))
        img.save(cached, "JPEG", quality=85)
    with open(cached, "rb") as f:
        return Response(f.read(), media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})


# ---------------------------------------------------------------- WebSocket (live progress)
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = ""):
    # The HTTP middleware does not see WebSockets: same network checks here
    client = websocket.client.host if websocket.client else ""
    if not _is_local_ip(client) or not _host_allowed(websocket.headers.get("host", "")):
        await websocket.close(code=4403)
        return
    if not check_token(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _sockets.add(websocket)
    active = [job_public(j) for j in jobs_load() if j["status"] in ("queued", "running")]
    await websocket.send_json({"event": "hello", "active_jobs": active})
    try:
        while True:
            await websocket.receive_text()  # keep the connection alive (the phone sends "ping")
    except WebSocketDisconnect:
        pass
    finally:
        _sockets.discard(websocket)


# ---------------------------------------------------------------- Wi-Fi advertising and keeping the Mac awake
def lan_ips():
    """Private IPv4 addresses of this Mac (no loopback or link-local): Wi-Fi/Ethernet (en*) first."""
    wifi, others = [], []   # physical interfaces first (en0, en1: Wi-Fi/Ethernet), then VPN, bridges...
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        iface = ""
        for line in out.splitlines():
            if line and not line[0].isspace():
                iface = line.split(":")[0]
            elif line.strip().startswith("inet "):
                (wifi if iface.startswith("en") else others).append(line.split()[1])
    except Exception:
        pass
    if not wifi:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.1.1", 9))  # sends nothing: only picks the outgoing interface
            wifi.append(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    def usable(ip):
        a = ipaddress.ip_address(ip)
        return _is_local_ip(ip) and not a.is_loopback and not a.is_link_local
    return [ip for ip in dict.fromkeys(wifi + others) if usable(ip)]


def lan_ip():
    ips = lan_ips()
    return ips[0] if ips else "127.0.0.1"


def advertise():
    """
    Advertises _spotmp3._tcp on the Wi-Fi with every local IP and redoes it if they change.
    Never dies: if something fails (no network at start-up, duplicate name after a quick restart...)
    it waits and starts over.
    """
    zc, info, current = None, None, None
    while True:
        try:
            ips = lan_ips()
            if ips != current:
                if zc:
                    try:
                        zc.unregister_all_services()
                        zc.close()
                    except Exception:
                        pass
                    zc, info = None, None
                if ips:
                    zc = Zeroconf()
                    info = ServiceInfo("_spotmp3._tcp.local.",
                                       f"spot-mp3 ({socket.gethostname().split('.')[0]})._spotmp3._tcp.local.",
                                       addresses=[socket.inet_aton(ip) for ip in ips], port=PORT,
                                       properties={"version": VERSION})
                    zc.register_service(info, allow_name_change=True)
                    log(f"anunciado en la WiFi: {', '.join(ips)} :{PORT}")
                else:
                    log("sin red local: se anunciará cuando la haya")
                current = ips
        except Exception as e:
            log(f"anuncio en la WiFi falló ({e}); se reintenta")
            current = None
            time.sleep(30)
            continue
        time.sleep(60)

def keep_awake_while(pid):
    """caffeinate -is: the Mac does not sleep while that process (the running job) lives."""
    subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(pid)])


async def startup():
    global _loop
    _loop = asyncio.get_running_loop()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=advertise, daemon=True).start()
    log(f"agente {VERSION} escuchando en el puerto {PORT}")


if __name__ == "__main__":
    os.chdir(HERE)
    if sys.argv[1:] == ["--install"]:
        rc, err = install_launch_agent(LABEL, __file__, "agent.log", RunAtLoad=True, KeepAlive=True, ThrottleInterval=10)
        print("Agente instalado: arranca solo al iniciar sesión." if rc == 0 else f"No se pudo activar: {err}")
        sys.exit(rc)
    if sys.argv[1:] == ["--uninstall"]:
        uninstall_launch_agent(LABEL)
        print("Agente quitado.")
        sys.exit(0)
    if sys.argv[1:2] == ["--unblock"]:
        # Lift pairing blocks (the running agent notices at once)
        ip = sys.argv[2] if len(sys.argv) > 2 else None
        try:
            removed = guard().unblock(ip)
        except OSError as e:
            print(f"No se pudo escribir {PAIR_BLOCKS_PATH} ({e}). Reinicia el agente para quitar los bloqueos: "
                  f"launchctl kickstart -k gui/$(id -u)/{LABEL}")
            sys.exit(1)
        print(f"Desbloqueados: {', '.join(removed)}." if removed else "No había bloqueos guardados.",
              "El agente olvida los fallos y bloqueos anteriores de " + (ip or "todos los dispositivos") + ".")
        sys.exit(0)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
