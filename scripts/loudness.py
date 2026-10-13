"""
Evens out the loudness of the MP3s (they come from YouTube, each at its own level) without
re-encoding them.

Like mp3gain: changes the global_gain field of every MP3 frame, in 1.5 dB steps. No quality is
lost, the file size stays the same and it can be undone. Works on any player (no ReplayGain
tags, which many MP3 players ignore).

  - Each song is measured with ffmpeg (EBU R128: integrated loudness in LUFS, true peak in dBTP).
  - Target: TARGET_LUFS. Louder songs go down; quieter ones go up only until the true peak
    reaches PEAK_CEILING (no clipping).
  - The applied gain is stored in the MP3 itself (TXXX:MIMUSICA_GAIN tag): never applied twice,
    and a new MP3 (download or studio version) is adjusted on the next run.

    python loudness.py            adjust the missing ones (runner.sh does it after "Versiones")
    python loudness.py --dry-run  measure and report what it would do
    python loudness.py --undo     undo the adjustment on every file

Exit codes: 0 ok (even if some could not be adjusted), 1 if ffmpeg is missing.
"""

import concurrent.futures
import glob
import json
import os
import re
import shutil
import subprocess
import sys

from mutagen.id3 import ID3, TXXX, ID3NoHeaderError

from common import SCRIPTS_DIR, ROOT, DATA_DIR, save_json

HERE = SCRIPTS_DIR
MUSIC_DIR = os.path.join(ROOT, "MP3 Songs")
ESSENTIA_CACHE = os.path.join(DATA_DIR, "essentia_features.json")
TARGET_LUFS = -11.0
PEAK_CEILING = -1.0       # maximum dBTP when raising a song
STEP_DB = 1.5             # one global_gain step
INAUDIBLE_GAIN = 40       # global_gain below this is inaudible (210 = unity gain)
TAG = "MIMUSICA_GAIN"     # "steps;LUFS measured before;target"
WORKERS = 6
FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.spotdl/ffmpeg")

_BITRATES = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
             2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


class Mp3Frames:
    """Walks the MPEG Layer III frames of an MP3 and locates their global_gain."""

    def __init__(self, data):
        self.data = data

    def audio_start(self):
        d = self.data
        if d[:3] == b"ID3" and len(d) >= 10:
            size = (d[6] & 0x7F) << 21 | (d[7] & 0x7F) << 14 | (d[8] & 0x7F) << 7 | (d[9] & 0x7F)
            return 10 + size + (10 if d[5] & 0x10 else 0)
        return 0

    def audio_end(self):
        d, end = self.data, len(self.data)
        if end >= 128 and d[end - 128:end - 125] == b"TAG":   # ID3v1
            end -= 128
        return end

    @staticmethod
    def parse_header(h):
        """(version 1/2/25, mono, crc, frame length) or None if it is not a valid Layer III frame."""
        if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
            return None
        ver_bits, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
        if ver_bits == 1 or layer != 1:
            return None
        version = {3: 1, 2: 2, 0: 25}[ver_bits]
        br_idx, sr_idx = h[2] >> 4, (h[2] >> 2) & 3
        if br_idx in (0, 15) or sr_idx == 3:
            return None
        bitrate = _BITRATES[1 if version == 1 else 2][br_idx] * 1000
        rate = _RATES[version][sr_idx]
        padding = (h[2] >> 1) & 1
        length = (144 if version == 1 else 72) * bitrate // rate + padding
        return version, (h[3] >> 6) == 3, not (h[1] & 1), length

    @staticmethod
    def gain_bits(version, mono):
        """Positions (in bits, from the start of the side info) of every global_gain in the frame."""
        if version == 1:
            start, granules = (18 if mono else 20), 2
            block = 59
        else:
            start, granules = (9 if mono else 10), 1
            block = 63
        channels = 1 if mono else 2
        return [start + (g * channels + c) * block + 21 for g in range(granules) for c in range(channels)]

    def frames(self):
        """(side info offset, global_gain positions) of every audio frame."""
        d, pos, end = self.data, self.audio_start(), self.audio_end()
        first, synced = True, True
        while pos + 4 <= end:
            info = self.parse_header(d[pos:pos + 4])
            if not info or pos + info[3] > end:
                pos, synced = pos + 1, False
                continue
            if not synced:
                # After a damaged chunk, some audio bytes can look like a header: only accept it if the next frame
                # also starts with a matching valid header
                nxt = self.parse_header(d[pos + info[3]:pos + info[3] + 4]) if pos + info[3] + 4 <= end else None
                if not nxt or nxt[:2] != info[:2] or (d[pos + 2] >> 2) & 3 != (d[pos + info[3] + 2] >> 2) & 3:
                    pos += 1
                    continue
                synced = True
            version, mono, crc, length = info
            side = pos + 4 + (2 if crc else 0)
            if crc:
                raise ValueError("tramas con CRC: no se tocan")
            if first and (b"Xing" in d[side:side + 40] or b"Info" in d[side:side + 40]):
                first = False   # LAME header frame (no audio; it has its own CRC)
            else:
                yield side, self.gain_bits(version, mono)
            first = False
            pos += length


def apply_gain(path, steps, tag_value=False):
    """
    Adds `steps` to global_gain in every frame (without re-encoding). If a granule would leave 0-255,
    `steps` is limited to what fits (apart from inaudible granules, nothing is ever clipped: undoing
    restores the file). tag_value: unless False, that tag is written (None = removed) in the same
    atomic change, so the gain is never applied without being recorded (or vice versa).
    Returns the steps applied.
    """
    with open(path, "rb") as f:
        data = bytearray(f.read())
    frames = list(Mp3Frames(data).frames())
    if len(frames) < 10:
        raise ValueError("no parece un MP3 Layer III")

    def field(bit, width):
        value = 0
        for i in range(width):
            byte, shift = (bit + i) // 8, 7 - (bit + i) % 8
            value = value << 1 | (data[byte] >> shift) & 1
        return value

    # Silent granules (big_values <= 1: almost no data, gain ~0) are left alone. The decision uses a field
    # this adjustment does not change, so undoing leaves the file identical
    targets = [side * 8 + b for side, bits in frames for b in bits if field(side * 8 + b - 9, 9) > 1]
    if not targets:
        steps = 0
    gains = [field(t, 8) for t in targets]
    if targets:
        # A gain < INAUDIBLE_GAIN is more than 250 dB below the maximum: it does not count for the limit and
        # may stay at 0 (undoing leaves it equally inaudible)
        audible = [g for g in gains if g >= INAUDIBLE_GAIN] or gains
        steps = max(-min(audible), min(255 - max(audible), steps))
    for t, gain in zip(targets, gains if steps else []):
        byte, shift = t // 8, t % 8
        word = data[byte] << 8 | data[byte + 1]
        word = (word & ~(0xFF << (8 - shift)) & 0xFFFF) | (min(255, max(0, gain + steps)) << (8 - shift))
        data[byte], data[byte + 1] = word >> 8, word & 0xFF
    if steps == 0 and tag_value is False:
        return 0
    tmp = path + ".gain.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    if tag_value is not False:
        write_tag(tmp, tag_value(steps) if callable(tag_value) else tag_value)
    os.replace(tmp, path)
    return steps


def measure(path):
    """(integrated LUFS, true peak dBTP) with ffmpeg."""
    out = subprocess.run([FFMPEG, "-nostats", "-hide_banner", "-i", path, "-filter_complex", "ebur128=peak=true",
                          "-f", "null", "-"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300).stderr
    summary = out[out.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    p = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if not i:
        raise ValueError("ffmpeg no ha dado la sonoridad")
    return float(i.group(1)), (float(p.group(1)) if p and p.group(1) != "-inf" else -99.0)


def steps_for(lufs, peak):
    steps = round((TARGET_LUFS - lufs) / STEP_DB)
    if steps > 0:   # raise only up to the peak ceiling
        steps = min(steps, max(0, int((PEAK_CEILING - peak) // STEP_DB)))
    return steps


def read_tag(path):
    try:
        frame = ID3(path).get(f"TXXX:{TAG}")
    except ID3NoHeaderError:
        return None
    return str(frame.text[0]) if frame else None


def write_tag(path, value):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if value is None:
        tags.delall(f"TXXX:{TAG}")
    else:
        tags.add(TXXX(encoding=3, desc=TAG, text=[value]))
    tags.save(path)


class EssentiaSignatures:
    """
    Essentia's analysis is redone when the MP3's size or date changes. Changing the gain does not
    change the music, so the signature in its cache is updated to avoid re-analysing.
    """

    def __init__(self, path=ESSENTIA_CACHE):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (FileNotFoundError, ValueError):
            self.cache = None
        self.changed = False

    def before(self, path):
        st = os.stat(path)
        return f"{st.st_size}-{int(st.st_mtime)}-"

    def after(self, path, old_prefix):
        if not self.cache or path not in self.cache:
            return
        sig = str(self.cache[path].get("signature", ""))
        if sig.startswith(old_prefix):
            st = os.stat(path)
            self.cache[path]["signature"] = f"{st.st_size}-{int(st.st_mtime)}-{sig[len(old_prefix):]}"
            self.changed = True

    def save(self):
        if self.changed:
            save_json(self.path, self.cache)


def refresh_playlists_folder(changed):
    """Adjusted MP3s are new files: "MP3 Player Card" (hard links) is brought up to date."""
    if not changed:
        return
    try:
        from mp3_export import Mp3Export
        Mp3Export().build()
    except Exception as e:
        print(f"Aviso: 'MP3 Player Card' no actualizada ({e}); ejecuta mp3_export.py")


class Normalizer:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.sigs = EssentiaSignatures()

    def _apply(self, path, steps, lufs=None):
        """Applies the gain and records the steps actually applied (lufs=None: undo)."""
        prefix = self.sigs.before(path)
        value = None if lufs is None else (lambda done: f"{done};{lufs:.1f};{TARGET_LUFS:g}")
        done = apply_gain(path, steps, tag_value=value)
        self.sigs.after(path, prefix)
        return done

    def run(self):
        files = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
        todo = [f for f in files if read_tag(f) is None]
        print(f"Volumen: {len(files)} MP3; se miden {len(todo)} (el resto ya ajustados). Objetivo {TARGET_LUFS:g} LUFS.")
        changed, errors, results = 0, 0, []
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
            for path, res in zip(todo, ex.map(self._measure_safe, todo)):
                if isinstance(res, Exception):
                    errors += 1
                    print(f"Aviso: no se pudo medir {os.path.basename(path)}: {res}")
                else:
                    results.append((path, *res))
        for path, lufs, peak in results:
            steps = steps_for(lufs, peak)
            if self.dry_run:
                print(f"  {lufs:6.1f} LUFS  pico {peak:5.1f}  {steps * STEP_DB:+5.1f} dB  {os.path.basename(path)}")
                continue
            try:
                if steps:
                    done = self._apply(path, steps, lufs)
                    changed += 1
                    if done != steps:
                        print(f"  {os.path.basename(path)}: {done * STEP_DB:+.1f} dB en vez de {steps * STEP_DB:+.1f} (no cabe más)")
                else:   # tag only (it also changes the file date: the signature is updated)
                    prefix = self.sigs.before(path)
                    write_tag(path, f"0;{lufs:.1f};{TARGET_LUFS:g}")
                    self.sigs.after(path, prefix)
            except Exception as e:
                errors += 1
                print(f"Aviso: no se pudo ajustar {os.path.basename(path)}: {e}")
        if not self.dry_run:
            self.sigs.save()
            refresh_playlists_folder(changed)
        if results:
            spread = [l for _, l, _ in results]
            print(f"Volumen: medidas entre {min(spread):.1f} y {max(spread):.1f} LUFS; "
                  f"{'se ajustarían' if self.dry_run else 'ajustados'} {changed if not self.dry_run else sum(steps_for(l, p) != 0 for _, l, p in results)}; "
                  f"{errors} con error.")
        return 0

    @staticmethod
    def _measure_safe(path):
        try:
            return measure(path)
        except Exception as e:
            return e

    def undo(self):
        n = 0
        for path in sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3"))):
            value = read_tag(path)
            if value is None:
                continue
            steps = int(value.split(";")[0])
            if self.dry_run:
                n += bool(steps)
                continue
            if steps:
                self._apply(path, -steps)
                n += 1
            else:
                prefix = self.sigs.before(path)
                write_tag(path, None)
                self.sigs.after(path, prefix)
        if self.dry_run:
            print(f"Volumen: se desharía en {n} MP3 (prueba: no se ha tocado nada).")
            return 0
        self.sigs.save()
        refresh_playlists_folder(n)
        print(f"Volumen: deshecho en {n} MP3.")
        return 0


def main(argv):
    if not os.path.exists(FFMPEG):
        print(f"Error: no se encuentra ffmpeg ({FFMPEG}).")
        return 1
    normalizer = Normalizer(dry_run="--dry-run" in argv)
    return normalizer.undo() if "--undo" in argv else normalizer.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
