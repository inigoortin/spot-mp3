"""
Analyses each MP3 locally with Essentia and its pre-trained models (models/ folder).

Metrics (0-1):
  danceability     danceability model (discogs-effnet)        -> P(danceable)
  acousticness     mood_acoustic model (discogs-effnet)       -> P(acoustic)
  instrumentalness voice_instrumental model (discogs-effnet)  -> P(instrumental)
  valence, energy  DEAM emotion model (msd-musicnn); energy = arousal. Scale 1-9 -> 0-1
  speechiness      YAMNet (AudioSet): share of rapped vs sung voice, Rapping / (Rapping + Singing)
                   (closer to Spotify's speechiness than the "Speech" probability in tests)
Also:
  genre_top        the 20 most likely Discogs styles (of 400) with their probability,
                   e.g. ["Latin---Reggaeton", 0.8]; enrich_spotify_metrics.py picks the genre
  key, mode        key and major/minor (KeyExtractor), camelot (DJ notation, e.g. "8A")
  bpm              tempo with RhythmExtractor2013 (multifeature) over the whole song at 44.1 kHz
                   (matched Spotify's tempo in 78 % of a 60-song test, vs 63 % for librosa)

Results go to data/essentia_features.json (key: MP3 path; recomputed when the file changes).
Run it with its own environment:
  .venv-essentia/bin/python essentia_analyzer.py            # every pending MP3
  .venv-essentia/bin/python essentia_analyzer.py a.mp3 ...  # only those files
"""

import glob
import json
import os
import sys

import essentia
import numpy as np

from common import ROOT, DATA_DIR, save_json

# Essentia prints ~25,000 "No network created..." warnings per song (70 MB logs): silence them
essentia.log.warningActive = False

from essentia.standard import (  # noqa: E402
    KeyExtractor,
    MonoLoader,
    RhythmExtractor2013,
    TensorflowPredict2D,
    TensorflowPredictEffnetDiscogs,
    TensorflowPredictMusiCNN,
    TensorflowPredictVGGish,
)

MUSIC_DIR = os.path.join(ROOT, "MP3 Songs")
MODELS_DIR = os.path.join(ROOT, "models")
CACHE_PATH = os.path.join(DATA_DIR, "essentia_features.json")
# MP3s that failed to analyse (path -> signature): not retried until the file changes
FAILED_PATH = os.path.join(DATA_DIR, "essentia_failed.json")
SAMPLE_RATE = 16000  # the one every model expects
ANALYZER_VERSION = 3  # bump it if the computation changes, to invalidate the cache

# Camelot notation: (key, mode) -> wheel code
CAMELOT = {
    ("B", "major"): "1B", ("F#", "major"): "2B", ("Db", "major"): "3B", ("Ab", "major"): "4B",
    ("Eb", "major"): "5B", ("Bb", "major"): "6B", ("F", "major"): "7B", ("C", "major"): "8B",
    ("G", "major"): "9B", ("D", "major"): "10B", ("A", "major"): "11B", ("E", "major"): "12B",
    ("Ab", "minor"): "1A", ("Eb", "minor"): "2A", ("Bb", "minor"): "3A", ("F", "minor"): "4A",
    ("C", "minor"): "5A", ("G", "minor"): "6A", ("D", "minor"): "7A", ("A", "minor"): "8A",
    ("E", "minor"): "9A", ("B", "minor"): "10A", ("F#", "minor"): "11A", ("C#", "minor"): "12A",
}
# KeyExtractor may return enharmonics; normalise them to the table's names
ENHARMONIC = {"C#": "Db", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb"}
ENHARMONIC_MINOR = {"Db": "C#", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb"}


def model(name):
    return os.path.join(MODELS_DIR, name)


def load_json(name):
    with open(model(name)) as f:
        return json.load(f)


class Analyzer:
    def __init__(self):
        self.effnet = TensorflowPredictEffnetDiscogs(graphFilename=model("discogs-effnet-bs64-1.pb"), output="PartitionedCall:1")
        self.musicnn = TensorflowPredictMusiCNN(graphFilename=model("msd-musicnn-1.pb"), output="model/dense/BiasAdd")
        self.danceability = TensorflowPredict2D(graphFilename=model("danceability-discogs-effnet-1.pb"), output="model/Softmax")
        self.acoustic = TensorflowPredict2D(graphFilename=model("mood_acoustic-discogs-effnet-1.pb"), output="model/Softmax")
        self.instrumental = TensorflowPredict2D(graphFilename=model("voice_instrumental-discogs-effnet-1.pb"), output="model/Softmax")
        self.genre = TensorflowPredict2D(
            graphFilename=model("genre_discogs400-discogs-effnet-1.pb"),
            input="serving_default_model_Placeholder",
            output="PartitionedCall:0",
        )
        self.deam = TensorflowPredict2D(graphFilename=model("deam-msd-musicnn-2.pb"), output="model/Identity")
        self.yamnet = TensorflowPredictVGGish(graphFilename=model("audioset-yamnet-1.pb"), input="melspectrogram", output="activations")
        self.key = KeyExtractor(sampleRate=SAMPLE_RATE)
        self.rhythm = RhythmExtractor2013(method="multifeature")

        self.genre_classes = load_json("genre_discogs400-discogs-effnet-1.json")["classes"]
        yamnet_classes = load_json("audioset-yamnet-1.json")["classes"]
        self.rap_idx = yamnet_classes.index("Rapping")
        self.sing_idx = yamnet_classes.index("Singing")

    def analyze_rhythm(self, path):
        audio = MonoLoader(filename=path, sampleRate=44100)()  # RhythmExtractor2013 requires 44.1 kHz
        bpm, _, confidence, _, _ = self.rhythm(audio)
        return {"bpm": round(float(bpm), 1), "bpm_confidence": float(confidence)}

    def analyze(self, path):
        audio = MonoLoader(filename=path, sampleRate=SAMPLE_RATE, resampleQuality=4)()

        emb = self.effnet(audio)
        danceable = float(np.mean(self.danceability(emb)[:, 0]))       # classes: danceable, not_danceable
        acoustic = float(np.mean(self.acoustic(emb)[:, 0]))            # classes: acoustic, non_acoustic
        instrumental = float(np.mean(self.instrumental(emb)[:, 0]))    # classes: instrumental, voice
        genre_probs = np.mean(self.genre(emb), axis=0)
        top = np.argsort(genre_probs)[::-1][:20]

        valence, arousal = np.mean(self.deam(self.musicnn(audio)), axis=0)  # DEAM scale 1-9

        events = self.yamnet(audio)
        rap, sing = events[:, self.rap_idx].sum(), events[:, self.sing_idx].sum()
        speechiness = float(rap / (rap + sing)) if rap + sing > 0 else 0.0

        key, scale, strength = self.key(audio)
        table = ENHARMONIC if scale == "major" else ENHARMONIC_MINOR
        key = table.get(key, key)

        return {
            **self.analyze_rhythm(path),
            "danceability": danceable,
            "acousticness": acoustic,
            "instrumentalness": instrumental,
            "valence": float(np.clip((valence - 1) / 8, 0, 1)),
            "energy": float(np.clip((arousal - 1) / 8, 0, 1)),
            "speechiness": speechiness,
            "genre_top": [[self.genre_classes[i], float(genre_probs[i])] for i in top],
            "key": key,
            "mode": scale,
            "key_strength": float(strength),
            "camelot": CAMELOT.get((key, scale), ""),
        }


def file_signature(path):
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}-v{ANALYZER_VERSION}"


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def load_failed():
    try:
        with open(FAILED_PATH) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if os.path.exists(k)} if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_failed(failed):
    save_json(FAILED_PATH, failed)


def save_cache(cache):
    save_json(CACHE_PATH, cache)


def main(files):
    files = [os.path.abspath(f) for f in files] or sorted(glob.glob(os.path.join(MUSIC_DIR, "**", "*.mp3"), recursive=True))
    cache = load_cache()
    if not sys.argv[1:]:
        pruned = {k: v for k, v in cache.items() if os.path.exists(k)}  # deleted MP3s
        if len(pruned) < len(cache):
            print(f"Se quitan {len(cache) - len(pruned)} entradas de MP3 que ya no existen.")
            cache = pruned
            save_cache(cache)
    failed = load_failed()
    pending = [f for f in files if cache.get(f, {}).get("signature") != file_signature(f)]
    skipped = [f for f in pending if failed.get(f) == file_signature(f)]
    if skipped:
        print(f"Se omiten {len(skipped)} MP3 que ya fallaron (bórralos de {os.path.basename(FAILED_PATH)} para reintentar): "
              + ", ".join(os.path.basename(f) for f in skipped[:5]))
    pending = [f for f in pending if f not in skipped]
    # Up-to-date entries that only lack the BPM (added later): compute only that
    rhythm_only = [f for f in files if f not in pending and f not in skipped and f in cache and "bpm" not in cache[f]
                   and failed.get(f) != file_signature(f)]
    print(f"{len(files)} MP3; {len(pending)} por analizar con Essentia"
          + (f", {len(rhythm_only)} solo BPM." if rhythm_only else "."))
    if not pending and not rhythm_only:
        return

    analyzer = Analyzer()
    work = [(p, False) for p in pending] + [(p, True) for p in rhythm_only]
    ok = 0
    for n, (path, only_rhythm) in enumerate(work, 1):
        try:
            result = analyzer.analyze_rhythm(path) if only_rhythm else analyzer.analyze(path)
        except Exception as e:
            print(f"  ❌ {os.path.basename(path)}: {e}")
            failed[path] = file_signature(path)
            save_failed(failed)
            continue
        ok += 1
        failed.pop(path, None)
        if only_rhythm:
            cache[path].update(result)
        else:
            result["signature"] = file_signature(path)
            cache[path] = result
        print(f"  {n}/{len(work)} {os.path.basename(path)}")
        if n % 10 == 0:
            save_cache(cache)
    save_cache(cache)
    save_failed(failed)
    errors = len(work) - ok
    if errors:
        print(f"Análisis: {ok}/{len(work)} bien, {errors} con error (ver arriba).")
    # Fail only if none succeeded (something general: models, installation...); one broken MP3 does not stop the rest
    return 1 if work and ok == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
