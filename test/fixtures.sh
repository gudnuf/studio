#!/usr/bin/env bash
# Realistic footage fixtures for the studio test + bench harness.
#
#   test/fixtures.sh [OUT_DIR]   (default: $STUDIO_FIXTURES or ~/.cache/studio-fixtures — a stable
#                                 path, so the cache survives `nix develop`'s fresh TMPDIR)
#
# Produces, in OUT_DIR:
#   phone-talking.mov    iPhone-style portrait talking head: HEVC (hvc1) stored 1920x1080 with a
#                        90-degree display matrix (displays 1080x1920), 30fps, ~45s, real speech
#   screen-recording.mp4 iPhone screen recording: 1170x2532 H.264, 25s, NO audio track, changing UI
#   site-walk.mov        landscape job walk: 1920x1080 HEVC, ~60s, speech + wind/traffic noise
#   voice-memo.m4a       audio-only AAC, ~8s
#   iphone-hdr.mov       what an iPhone records by default: 4K30 HEVC Main10, 10-bit HLG
#                        (BT.2020, arib-std-b67), landscape, 90s, ~40 Mb/s, speech + noise.
#                        The heavy path: VideoToolbox decode + HDR->SDR on every proxy/export.
#   junk.txt             not media; ingest must skip it
#   bursts.mov           quality fixture: portrait, three spoken sentences with known silences
#                        between them (room tone), and 0.36s of black picture mid second gap;
#                        bursts.json has the spans (speech, black) in seconds
#   broll-roomtone.mov   quality fixture: 8s of moving picture over room tone, nobody talking
#                        (whisper alone hears "Thank you." in it)
#
# Cached by content: a stamp of this script's bytes + tool versions lives in OUT_DIR/.stamp;
# reruns with an unchanged script are instant. Prints OUT_DIR on stdout (last line).
# Needs ffmpeg/ffprobe (run inside `nix develop`) and macOS `say`.
set -euo pipefail

out=${1:-${STUDIO_FIXTURES:-${XDG_CACHE_HOME:-$HOME/.cache}/studio-fixtures}}
mkdir -p "$out"
out=$(cd "$out" && pwd)

for t in ffmpeg ffprobe say shasum; do
  command -v "$t" >/dev/null || { echo "fixtures.sh: '$t' not on PATH (run inside nix develop)" >&2; exit 2; }
done

files=(phone-talking.mov screen-recording.mp4 site-walk.mov voice-memo.m4a iphone-hdr.mov junk.txt
       bursts.mov bursts.json broll-roomtone.mov)
stamp=$( { cat "${BASH_SOURCE[0]}"; ffmpeg -version | head -1; } | shasum -a 256 | cut -c1-16)
if [[ -f "$out/.stamp" && "$(cat "$out/.stamp")" == "$stamp" ]]; then
  ok=1; for f in "${files[@]}"; do [[ -s "$out/$f" ]] || ok=0; done
  if [[ $ok == 1 ]]; then echo "fixtures: cached ($stamp)" >&2; echo "$out"; exit 0; fi
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/studio-fx.XXXXXX")
trap 'rm -rf "$work"' EXIT
FONT=/System/Library/Fonts/Helvetica.ttc
[[ -f $FONT ]] || FONT=/System/Library/Fonts/SFNS.ttf
ff() { ffmpeg -hide_banner -nostdin -y -v error "$@"; }

# HEVC: hardware first (what an iPhone produces is HEVC; VideoToolbox is what we'd use), x265 fallback.
hevc() {  # hevc IN_ARGS... -- OUT
  local args=() o
  while [[ $1 != -- ]]; do args+=("$1"); shift; done; shift; o=$1
  ff "${args[@]}" -c:v hevc_videotoolbox -b:v 8M -tag:v hvc1 -pix_fmt yuv420p "$o" 2>/dev/null \
    || ff "${args[@]}" -c:v libx265 -preset fast -crf 26 -tag:v hvc1 -pix_fmt yuv420p \
          -x265-params log-level=error "$o"
}

echo "fixtures: generating into $out" >&2

# --- speech ------------------------------------------------------------------
say -v Samantha -r 175 -o "$work/talk.aiff" "Hey, I'm the guy who builds Jefe. \
Quick story. Last week I walked a property with forty sprinkler heads, and I realized \
I used to spend two hours every single night typing up estimates after the job. \
Two hours. Every night. That's time I wasn't with my kids. \
So here's what I do now. I just talk through the job while I walk it. \
Front beds need mulch, call it three yards. Zone two has a broken head, replace it. \
Trim the hedges along the driveway. \
By the time I'm back at the truck, the estimate is written, line by line, priced. \
And the part that matters to me: the audio never leaves my phone. \
It's my business. My customers. My data. \
If you run a crew and you're still doing paperwork at the kitchen table, try it this week. \
Tell me what breaks. I read every message."

say -v Daniel -r 165 -o "$work/walk.aiff" "Alright, we're on site at the Henderson property. \
Let me walk you through how I'd quote this. \
Starting at the front. These beds along the walkway, the mulch is basically gone, \
so that's about four yards of hardwood mulch, plus edging. \
Over here, see this head? Cracked. That's zone three. Replace the head, check the line. \
Coming around the side of the house now. The gate sticks, not our job, but I'll note it for the owner. \
Backyard. Big lawn, maybe six thousand square feet. Aeration and overseed in the fall. \
These two shrubs are dead, pull them and replace with something drought tolerant. \
And that's the whole walk. Normally I'd go home and type all of this up. \
Instead I just said it out loud, and Jefe already turned it into line items. \
I'll review it in the truck, tweak the mulch price, and send it before I leave the driveway."

say -v Samantha -r 180 -o "$work/memo.aiff" "Note to self. Film the before and after on the Henderson beds. \
Ask Maria if we can use her yard for the demo."

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1"; }

# --- phone-talking.mov: portrait talking head, stored landscape + rotate 90 ---------------
# Content is drawn upright for portrait viewing, then transposed so the coded frame is
# 1920x1080 like an iPhone sensor; the display matrix rotates it back on playback.
T=45
ff -f lavfi -i "color=c=0x6b5a48:size=1080x1920:rate=30:duration=$T" \
   -f lavfi -i "color=c=0xd9b38c:size=420x520:rate=30:duration=$T" \
   -filter_complex "[0][1]overlay=x='330+18*sin(t*1.3)':y='560+10*sin(t*2.1)'[v];\
[v]drawtext=fontfile=$FONT:text='PHONE TALKING HEAD':fontsize=56:fontcolor=white:x=(w-tw)/2:y=180,\
drawtext=fontfile=$FONT:text='%{pts\\:hms}':fontsize=64:fontcolor=white:x=(w-tw)/2:y=h-300,\
transpose=1[out]" \
   -map "[out]" -an -c:v libx264 -preset ultrafast -crf 18 "$work/talk_land.mp4"
ff -i "$work/talk.aiff" -af "apad" -t $T -ar 48000 -ac 1 -c:a aac -b:a 128k "$work/talk.m4a"
hevc -i "$work/talk_land.mp4" -i "$work/talk.m4a" -map 0:v -map 1:a -c:a copy -r 30 -- "$work/talk_hevc.mov"
# Rotation metadata, written the way an iPhone does it (display matrix on the video track).
ff -display_rotation:v:0 90 -i "$work/talk_hevc.mov" -map 0 -c copy -tag:v hvc1 \
   -metadata creation_time="2026-11-08T09:12:00Z" -metadata:s:v handler_name="Core Media Video" \
   "$out/phone-talking.mov"

# --- screen-recording.mp4: 1170x2532, silent, UI that visibly changes --------------------
S=25
ff -f lavfi -i "color=c=0xf2f2f7:size=1170x2532:rate=30:duration=$S" -vf "\
drawbox=x=0:y=0:w=iw:h=260:color=0x1c1c1e:t=fill,\
drawtext=fontfile=$FONT:text='Jefe':fontsize=90:fontcolor=white:x=60:y=110,\
drawtext=fontfile=$FONT:text='Estimate #%{eif\\:1041+floor(t/5)\\:d}':fontsize=70:fontcolor=0x1c1c1e:x=60:y=340,\
drawbox=x=60:y='520+mod(t*120\\,1500)':w=1050:h=180:color=0x0a84ff:t=fill,\
drawbox=x=60:y=760:w='min(1050\\,t*60)':h=40:color=0x34c759:t=fill,\
drawtext=fontfile=$FONT:text='Front beds - mulch 3 yd':fontsize=54:fontcolor=0x3a3a3c:x=60:y=900+mod(t*40\\,800),\
drawtext=fontfile=$FONT:text='Zone 2 head - replace':fontsize=54:fontcolor=0x3a3a3c:x=60:y=1000+mod(t*40\\,800),\
drawtext=fontfile=$FONT:text='%{pts\\:hms}':fontsize=48:fontcolor=0x8e8e93:x=w-tw-60:y=h-140,\
hue=h='mod(t*14\\,360)':s=1" \
   -an -c:v h264_videotoolbox -b:v 10M -pix_fmt yuv420p -profile:v high "$out/screen-recording.mp4" 2>/dev/null \
 || ff -f lavfi -i "testsrc2=size=1170x2532:rate=30:duration=$S" -an -c:v libx264 -preset fast \
       -pix_fmt yuv420p "$out/screen-recording.mp4"

# --- site-walk.mov: landscape walk, speech + outdoor noise -----------------------------
W=60
ff -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=$W" -vf "\
scroll=horizontal=0.002:vertical=0.0005,eq=saturation=0.6,\
drawtext=fontfile=$FONT:text='SITE WALK - HENDERSON':fontsize=64:fontcolor=white:box=1:boxcolor=black@0.5:x=60:y=60,\
drawtext=fontfile=$FONT:text='%{pts\\:hms}':fontsize=56:fontcolor=white:box=1:boxcolor=black@0.5:x=w-tw-60:y=h-120" \
   -an -c:v libx264 -preset ultrafast -crf 20 "$work/walk_v.mp4"
ff -i "$work/walk.aiff" -f lavfi -i "anoisesrc=color=pink:amplitude=0.06:duration=$W:sample_rate=48000" \
   -f lavfi -i "anoisesrc=color=brown:amplitude=0.12:duration=$W:sample_rate=48000" \
   -filter_complex "[0]aresample=48000,apad,atrim=0:$W,volume=1.6[s];[1]highpass=f=900[wind];\
[2]lowpass=f=200,volume='0.5+0.5*sin(t*0.4)':eval=frame[traffic];\
[s][wind][traffic]amix=inputs=3:duration=first:normalize=0,alimiter=limit=0.95[a]" \
   -map "[a]" -ac 2 -c:a aac -b:a 160k "$work/walk.m4a"
hevc -i "$work/walk_v.mp4" -i "$work/walk.m4a" -map 0:v -map 1:a -c:a copy -- "$out/site-walk.mov"

# --- iphone-hdr.mov: 4K HLG 10-bit, like the iPhone default ----------------------------
H=90
ff -stream_loop 2 -i "$work/walk.m4a" -t $H -c:a aac -b:a 160k "$work/hdr.m4a"
ff -f lavfi -i "testsrc2=size=3840x2160:rate=30:duration=$H,noise=alls=10:allf=t,\
drawtext=fontfile=$FONT:text='IPHONE HDR 4K HLG':fontsize=120:fontcolor=white:box=1:boxcolor=black@0.5:x=120:y=120,\
format=p010le" -i "$work/hdr.m4a" -map 0:v -map 1:a -c:v hevc_videotoolbox -profile:v main10 -b:v 40M \
   -tag:v hvc1 -color_primaries bt2020 -color_trc arib-std-b67 -colorspace bt2020nc -c:a copy -shortest \
   "$out/iphone-hdr.mov" 2>/dev/null \
 || ff -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=$H,format=yuv420p10le" -i "$work/hdr.m4a" \
      -map 0:v -map 1:a -c:v libx265 -preset ultrafast -x265-params log-level=error -tag:v hvc1 \
      -color_primaries bt2020 -color_trc arib-std-b67 -colorspace bt2020nc -c:a copy -shortest "$out/iphone-hdr.mov"

# --- bursts.mov + bursts.json: speech with known gaps, a black stretch ----------------------
# Each sentence is trimmed to its own sound, then laid out on room tone with set silences, so
# where the speech is (to ~30 ms) is known by construction.
say -v Samantha -r 175 -o "$work/b1.aiff" "Last week I walked a property with forty sprinkler heads."
say -v Samantha -r 175 -o "$work/b2.aiff" "I used to spend two hours every night typing up estimates."
say -v Samantha -r 175 -o "$work/b3.aiff" "Now the estimate is written before I reach the truck."
trim="silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse"
for i in 1 2 3; do ff -i "$work/b$i.aiff" -af "aresample=48000,$trim" -ac 1 -c:a pcm_s16le "$work/b$i.wav"; done
gaps=(0.8 1.2 0.9 1.5)                   # before sentence 1, between 1-2, 2-3, after 3
t=${gaps[0]} spans="" parts=()
for i in 1 2 3; do
  d=$(dur "$work/b$i.wav")
  ff -f lavfi -i "anullsrc=r=48000:cl=mono" -t "${gaps[$((i - 1))]}" -c:a pcm_s16le "$work/g$i.wav"
  parts+=("$work/g$i.wav" "$work/b$i.wav")
  spans+="${spans:+, }[$t, $(echo "$t + $d" | bc -l)]"
  t=$(echo "$t + $d + ${gaps[$i]}" | bc -l)
done
ff -f lavfi -i "anullsrc=r=48000:cl=mono" -t "${gaps[3]}" -c:a pcm_s16le "$work/g4.wav"
parts+=("$work/g4.wav")
printf "file '%s'\n" "${parts[@]}" > "$work/bursts.txt"
B=$(echo "$t" | bc -l)
ff -f concat -safe 0 -i "$work/bursts.txt" -f lavfi -i "anoisesrc=color=brown:amplitude=0.004:duration=$B:sample_rate=48000" \
   -filter_complex "[0][1]amix=inputs=2:duration=first:normalize=0[a]" -map "[a]" -c:a pcm_s16le "$work/bursts.wav"
b2end=$(python3 -c "import json; print(json.loads('[${spans}]')[1][1])")
k0=$(echo "$b2end + 0.42" | bc -l); k1=$(echo "$b2end + 0.78" | bc -l)   # clear of snapped tails/lead-ins
ff -f lavfi -i "testsrc2=size=1080x1920:rate=30:duration=$B" -i "$work/bursts.wav" \
   -vf "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,$k0,$k1)'" \
   -map 0:v -map 1:a -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -shortest "$out/bursts.mov"
printf '{"speech": [%s], "black": [%s, %s]}\n' "$spans" "$k0" "$k1" > "$out/bursts.json"

# --- broll-roomtone.mov: picture over room tone, no speech ---------------------------------
ff -f lavfi -i "testsrc2=size=1080x1920:rate=30:duration=8" \
   -f lavfi -i "anoisesrc=color=brown:amplitude=0.01:duration=8:sample_rate=48000" \
   -map 0:v -map 1:a -c:v libx264 -preset ultrafast -crf 20 -pix_fmt yuv420p -c:a aac -b:a 128k "$out/broll-roomtone.mov"

# --- voice-memo.m4a + junk.txt ---------------------------------------------------------
ff -i "$work/memo.aiff" -ar 48000 -ac 1 -c:a aac -b:a 96k "$out/voice-memo.m4a"
printf 'not footage — studio ingest must skip this\n' > "$out/junk.txt"

for f in "${files[@]}"; do [[ -s "$out/$f" ]] || { echo "fixtures: $f missing" >&2; exit 1; }; done
echo "$stamp" > "$out/.stamp"
for f in phone-talking.mov screen-recording.mp4 site-walk.mov voice-memo.m4a iphone-hdr.mov; do
  printf 'fixtures: %-22s %6.1fs  %s\n' "$f" "$(dur "$out/$f")" \
    "$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height -of csv=p=0 "$out/$f" 2>/dev/null | head -1)" >&2
done
echo "$out"
