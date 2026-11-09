#!/usr/bin/env bash
# Synthetic footage for an end-to-end smoke test: a portrait "phone talking" clip with
# real speech (macOS `say`) and a silent landscape "screen recording".
set -euo pipefail
out=${1:-$(mktemp -d)}; mkdir -p "$out"
say -o "$out/speech.aiff" "Last week I walked a property with forty sprinkler heads. \
I used to spend two hours every night typing up estimates after the job. \
With Jefe I just talked through it, front beds need mulch, call it three yards, zone two head is broken, replace it. \
By the time I got back to the truck the estimate was written. \
And the audio never left my phone. That part matters to me."
ffmpeg -y -v error -f lavfi -i "testsrc2=size=1080x1920:rate=30" -i "$out/speech.aiff" \
  -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac "$out/talking.mov"
ffmpeg -y -v error -f lavfi -i "testsrc=size=1920x1080:rate=30:duration=20" \
  -c:v libx264 -pix_fmt yuv420p "$out/screen-recording.mp4"
rm "$out/speech.aiff"
echo "$out"
