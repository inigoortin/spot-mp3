#!/bin/bash
# Downloads the pre-trained Essentia models used by essentia_analyzer.py into models/ (~40 MB).
# Files already present are skipped.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="https://essentia.upf.edu/models"
FILES=(
    feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb
    feature-extractors/musicnn/msd-musicnn-1.pb
    classification-heads/danceability/danceability-discogs-effnet-1.pb
    classification-heads/mood_acoustic/mood_acoustic-discogs-effnet-1.pb
    classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.pb
    classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb
    classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json
    classification-heads/deam/deam-msd-musicnn-2.pb
    audio-event-recognition/yamnet/audioset-yamnet-1.pb
    audio-event-recognition/yamnet/audioset-yamnet-1.json
)
mkdir -p "$DIR/models"
for f in "${FILES[@]}"; do
    out="$DIR/models/$(basename "$f")"
    if [ -s "$out" ]; then echo "ok    $(basename "$f")"; continue; fi
    echo "get   $(basename "$f")"
    curl -fsSL "$BASE/$f" -o "$out.part" && mv "$out.part" "$out"
done
echo "Models ready in $DIR/models"
