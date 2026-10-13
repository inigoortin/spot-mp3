"""
Protection of phone pairing (used by agent_server.py).

- Each 6-digit code expires after 5 min; a new code from a device replaces its pending one. It can
  only be confirmed from the same device that asked for it.
- Per device (IP address):
    * more than 3 codes in 10 min -> refused and reported on the Mac;
      keep asking (5 more refusals) -> device blocked for 24 h;
    * 5 wrong codes in 24 h -> device blocked for 24 h (warning from the 3rd).
  A block only affects that device, so nobody can lock you out of pairing on purpose.
- Across all devices it only warns (blocks nobody else): many failures in 1 h or many devices
  asking at once mean someone is trying from several addresses. With 5 attempts per address every
  24 h, guessing a 6-digit code would take years.
- Blocks are saved in data/private/.agent_pair_blocks.json (they survive agent restarts) and each
  one is notified on the Mac. To lift them (also with the agent running: it notices at once and
  forgets earlier failures):
      python agent_server.py --unblock         all
      python agent_server.py --unblock <ip>    one
"""

import fcntl
import json
import math
import os
import secrets
import sys
import threading
import time
import uuid

from common import SCRIPTS_DIR, save_json

HOUR, DAY = 3600, 86400
UNBLOCK_HINT = f'cd "{SCRIPTS_DIR}" && "{sys.executable}" agent_server.py --unblock'


class PairingRefused(Exception):
    """Pairing request refused: carries the HTTP status and the message for the phone."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class BlockStore:
    """
    .agent_pair_blocks.json: {"ips": {ip: {"until", "reason"}}, "unblocked": {ip|"*": instant}}.
    "unblocked" records when a manual unblock happened: earlier failures stop counting.
    """

    def __init__(self, path, log, clock=time.time, keep_stamps=DAY):
        self.path, self.log, self.clock, self.keep_stamps = path, log, clock, keep_stamps

    def locked(self):
        """Process lock (agent and --unblock) for read-modify-write without clobbering."""
        return _FileLock(self.path + ".lock")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except ValueError:
            self.log(f"SEGURIDAD: {self.path} ilegible; se ignora")
            data = {}
        if not isinstance(data, dict):
            data = {}
        ips = data.get("ips") if isinstance(data.get("ips"), dict) else {}
        ips = {ip: e for ip, e in ips.items() if isinstance(e, dict) and _finite(e.get("until"))}
        unblocked = data.get("unblocked") if isinstance(data.get("unblocked"), dict) else {}
        unblocked = {k: v for k, v in unblocked.items() if _finite(v)}
        return {"ips": ips, "unblocked": unblocked}

    def save(self, data):
        now = self.clock()   # drop what expired: past blocks and marks that no longer affect anything
        data = {"ips": {ip: e for ip, e in data["ips"].items() if e["until"] > now},
                "unblocked": {k: v for k, v in data["unblocked"].items() if now - v < self.keep_stamps}}
        save_json(self.path, data, private=True)


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v < 1e11


class _FileLock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


class PairingGuard:
    CODE_TTL = 300
    MAX_PENDING = 50
    START_WINDOW = 600
    STARTS_PER_IP = 3              # codes per device and window
    REFUSED_STARTS_TO_BLOCK = 5
    WRONG_WINDOW = DAY
    WRONG_PER_IP = 5               # wrong codes per device before blocking
    WRONG_ALERT_FROM = 3
    IP_BLOCK = DAY
    GLOBAL_WINDOW = HOUR
    GLOBAL_WRONG_ALERT = 10        # failures across all devices in 1 h -> warning
    PENDING_IPS_ALERT = 4          # different devices with a pending code at once -> warning
    ALERT_EVERY = HOUR             # at most one warning of each kind per device and hour

    def __init__(self, blocks_path, alert, log, clock=time.time):
        """alert(text): Mac notification; log(text): log line; clock: for tests."""
        self.store = BlockStore(blocks_path, log, clock, keep_stamps=self.WRONG_WINDOW)
        self.alert, self.log, self.clock = alert, log, clock
        self._lock = threading.Lock()
        self._pending = {}       # pairing_id -> {code, expires, device, ip}
        self._starts = {}        # ip -> [instants]
        self._refused = {}       # ip -> [instants of requests refused for excess]
        self._wrong = {}         # ip -> [instants of wrong codes]
        self._alerted = {}       # (ip, kind) -> instant of the last warning
        self._outbox = []        # warnings to send after releasing the lock (osascript is slow)
        self._applied = {}       # ip|"*" -> unblock mark already applied
        self._mem_blocks = {}    # ip -> end of the block, if it could not be saved to the file

    # ------------------------------------------------------------ API
    def start(self, ip, device):
        """New code for `ip`. Returns (pairing_id, code). Raises PairingRefused."""
        try:
            with self._lock:
                return self._start(ip, device)
        finally:
            self._flush_alerts()

    def finish(self, ip, pairing_id, code):
        """Checks the code. Returns the device name if correct. Raises PairingRefused."""
        try:
            with self._lock:
                return self._finish(ip, pairing_id, code)
        finally:
            self._flush_alerts()

    def unblock(self, ip=None):
        """
        Lifts the block of one device (or all) and records when: the running agent (another process)
        forgets earlier failures when it reads it. Returns the unblocked devices.
        """
        with self._lock, self.store.locked():
            data = self.store.load()
            removed = list(data["ips"]) if ip is None else [i for i in data["ips"] if i == ip]
            for i in removed:
                data["ips"].pop(i)
            data["unblocked"][ip or "*"] = self.clock()
            self.store.save(data)
            self._apply_unblocks(data)
            return removed

    # ------------------------------------------------------------ internals (lock held)
    def _start(self, ip, device):
        now = self.clock()
        data = self._refresh(now)
        self._check_blocked(ip, data, now)
        if len(self._starts.get(ip, [])) >= self.STARTS_PER_IP:
            refused = self._refused.setdefault(ip, [])
            refused.append(now)
            if len(refused) >= self.REFUSED_STARTS_TO_BLOCK:
                self._block_ip(ip, "pedir códigos sin parar", now)
            else:
                self._queue_alert(ip, "starts", f"{ip} ({device}) está pidiendo muchos códigos de emparejado", now)
            raise PairingRefused(429, "has pedido demasiados códigos; espera unos minutos")
        self._pending = {k: v for k, v in self._pending.items() if v["ip"] != ip}
        if len(self._pending) >= self.MAX_PENDING:
            self._queue_alert("*", "pending_cap", f"Emparejado saturado: {len(self._pending)} códigos pendientes "
                                                  f"de dispositivos distintos; nadie más puede emparejar ahora", now)
            raise PairingRefused(429, "demasiados emparejados pendientes; inténtalo en unos minutos")
        self._starts.setdefault(ip, []).append(now)
        pairing_id, code = uuid.uuid4().hex, f"{secrets.randbelow(10**6):06d}"
        self._pending[pairing_id] = {"code": code, "expires": now + self.CODE_TTL, "device": device, "ip": ip}
        ips = {v["ip"] for v in self._pending.values()}
        if len(ips) >= self.PENDING_IPS_ALERT:
            self._queue_alert("*", "pending", f"{len(ips)} dispositivos distintos están pidiendo códigos de "
                                              f"emparejado a la vez: {', '.join(sorted(ips))}", now)
        return pairing_id, code

    def _finish(self, ip, pairing_id, code):
        now = self.clock()
        data = self._refresh(now)
        self._check_blocked(ip, data, now)
        p = self._pending.get(pairing_id)
        if not p or p["ip"] != ip:
            raise PairingRefused(410, "código caducado; vuelve a empezar")
        code = str(code).strip()
        valid_form = len(code) == 6 and code.isascii() and code.isdigit()
        if valid_form and secrets.compare_digest(code.encode(), p["code"].encode()):
            self._pending.pop(pairing_id)
            self._wrong.pop(ip, None)
            return p["device"]
        wrong = self._wrong.setdefault(ip, [])
        wrong.append(now)
        if len(wrong) >= self.WRONG_PER_IP:
            self._block_ip(ip, f"{len(wrong)} códigos incorrectos", now)
        elif len(wrong) >= self.WRONG_ALERT_FROM:
            self._queue_alert(ip, "wrong", f"{ip} ({p['device']}) ha fallado {len(wrong)} códigos de emparejado", now)
        total = sum(1 for times in self._wrong.values() for t in times if now - t < self.GLOBAL_WINDOW)
        if total >= self.GLOBAL_WRONG_ALERT:
            self._queue_alert("*", "global", f"{total} códigos de emparejado incorrectos en la última hora desde "
                                             f"{len(self._wrong)} dispositivos: posible intento de entrar", now)
        raise PairingRefused(403, "código incorrecto")

    def _refresh(self, now):
        """Reads the blocks, applies unblocks made from outside and cleans up what expired."""
        data = self.store.load()
        self._apply_unblocks(data)
        self._pending = {k: v for k, v in self._pending.items() if v["expires"] >= now}
        for table, window in ((self._starts, self.START_WINDOW), (self._refused, self.START_WINDOW),
                              (self._wrong, self.WRONG_WINDOW)):
            for ip in list(table):
                table[ip] = [t for t in table[ip] if now - t < window]
                if not table[ip]:
                    table.pop(ip)
        self._alerted = {k: t for k, t in self._alerted.items() if now - t < self.ALERT_EVERY}
        return data

    def _apply_unblocks(self, data):
        """
        Forgets counters and warnings older than each manual unblock. Each mark is applied once, and at
        most up to "now": a mark in the future (clock ahead when unblocking) cannot keep erasing new
        failures.
        """
        now = self.clock()
        for key, stamp in data["unblocked"].items():
            if self._applied.get(key) == stamp:
                continue
            self._applied[key] = stamp
            since = min(stamp, now)
            for ip in [i for i in self._mem_blocks if key == "*" or i == key]:
                self._mem_blocks.pop(ip)
            for table in (self._starts, self._refused, self._wrong):
                for ip in [i for i in table if key == "*" or i == key]:
                    table[ip] = [t for t in table[ip] if t > since]
                    if not table[ip]:
                        table.pop(ip)
            for ip, kind in [k for k in self._alerted if key == "*" or k[0] == key]:
                if self._alerted[(ip, kind)] <= since:
                    self._alerted.pop((ip, kind))

    def _check_blocked(self, ip, data, now):
        entry = data["ips"].get(ip)
        if self._mem_blocks.get(ip, 0) > (entry["until"] if entry else 0):   # the longer of the two
            entry = {"until": self._mem_blocks[ip], "reason": "intentos sospechosos"}
        if entry and now < entry["until"]:
            hours = -int((now - entry["until"]) // 3600)   # round up
            raise PairingRefused(429, f"este dispositivo está bloqueado {hours} h por "
                                      f"{entry.get('reason', 'intentos sospechosos')}; desbloquéalo en el Mac")

    def _block_ip(self, ip, reason, now):
        # Block in memory first: if the file cannot be written, it stays blocked
        self._mem_blocks[ip] = now + self.IP_BLOCK
        self._pending = {k: v for k, v in self._pending.items() if v["ip"] != ip}
        self._queue_alert(ip, "block", f"Emparejado bloqueado 24 h para {ip}: {reason}. "
                                       f"Si eras tú, en el Terminal: {UNBLOCK_HINT} {ip}", now)
        try:
            with self.store.locked():
                data = self.store.load()
                data["ips"][ip] = {"until": now + self.IP_BLOCK, "reason": reason}
                self.store.save(data)
        except OSError as e:
            self._queue_alert(ip, "store", f"No se pudo guardar el bloqueo de {ip} ({e}); se mantiene hasta "
                                            f"reiniciar el agente", now)

    def _queue_alert(self, ip, kind, text, now):
        self.log(f"SEGURIDAD: {text}")
        if now - self._alerted.get((ip, kind), -self.ALERT_EVERY) >= self.ALERT_EVERY:
            self._alerted[(ip, kind)] = now
            self._outbox.append(text)

    def _flush_alerts(self):
        """Sends warnings outside the lock: a slow notification does not hold up other requests."""
        with self._lock:
            texts, self._outbox = self._outbox, []
        for text in texts:
            try:
                self.alert(text)
            except Exception as e:
                self.log(f"SEGURIDAD: no se pudo mostrar el aviso ({e})")
