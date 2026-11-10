#!/usr/bin/env bash
# studio: fixtures -> CLI tests -> server tests -> UI smoke (headless Chrome) -> Rust workspace
# -> benchmark.
# Exits nonzero on any failure.
#
#   test/run_all.sh [--no-bench] [--no-ui] [--strict] [--only cli|server|ui|webkit|app|rust|bench]
#
#   ui      the operator path in headless Chrome
#   webkit  the same core path in Safari (safaridriver; skipped until `sudo safaridriver --enable`)
#   app     the built Mac app in its WKWebView (skipped until `nix build .#studio-app`; uses result/)
#   rust    `cargo test --workspace` + clippy (-D warnings) over crates/, with the nix toolchain
#
#   --strict     bench budget misses (SLOW) also fail the run
#   STUDIO_TEST_TMP   where temp homes, logs and bench.json go (default: a fresh mktemp dir)
#   STUDIO_FIXTURES   fixture cache dir (default: ~/.cache/studio-fixtures; reused across runs)
#   STUDIO_TEST_PORTS port range servers and Chrome may use (default 7400-7499)
#
# Re-enters itself inside `nix develop` when ffmpeg/whisper-cli aren't on PATH, or when the Rust
# step will run and cargo isn't the pinned nix toolchain (nix/rust.nix).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

args=("$@")
bench=1 ui=1 strict="" only=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --no-bench) bench=0 ;;
    --no-ui) ui=0 ;;
    --strict) strict=--strict ;;
    --only) only=$2; shift ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done

if ! command -v ffmpeg >/dev/null || ! command -v whisper-cli >/dev/null \
   || { [[ -z $only || $only == rust ]] && [[ $(command -v cargo) != /nix/store/* ]]; }; then
  exec nix develop "$root" --quiet -c bash "$here/run_all.sh" "${args[@]}" 2> >(grep -v "Git tree .* is dirty" >&2)
fi

export STUDIO_TEST_TMP=${STUDIO_TEST_TMP:-$(mktemp -d "${TMPDIR:-/tmp}/studio-test.XXXXXX")}
export STUDIO_FIXTURES=${STUDIO_FIXTURES:-${XDG_CACHE_HOME:-$HOME/.cache}/studio-fixtures}
mkdir -p "$STUDIO_TEST_TMP"
logs="$STUDIO_TEST_TMP/logs"; mkdir -p "$logs"
# tests say explicitly when they are the agent, and which agent the AI steps call
unset CLAUDECODE CODEX_THREAD_ID CODEX_SANDBOX GOOSE_TERMINAL STUDIO_ACTOR \
  STUDIO_AGENT STUDIO_AGENT_CMD STUDIO_AGENT_MODEL STUDIO_MOMENTS_MODEL STUDIO_CLAUDE_MODEL
cd "$root"

declare -a names results times
fail=0
run() {  # run NAME CMD...
  local name=$1; shift
  [[ -n $only && $name != "$only" && ( $name != fixtures || $only == rust ) ]] && return 0
  local t0=$SECONDS
  echo "━━ $name"
  "$@" 2>&1 | tee "$logs/$name.log"
  local rc=${PIPESTATUS[0]}
  names+=("$name"); results+=("$rc"); times+=($((SECONDS - t0)))
  [[ $rc -ne 0 ]] && fail=1
  return 0
}

run fixtures bash "$here/fixtures.sh" "$STUDIO_FIXTURES"
if [[ ${names[0]:-} == fixtures && ${results[0]} -ne 0 ]]; then echo "fixtures failed; stopping" >&2; exit 1; fi
run cli    python3 "$here/test_cli.py" -v
run server python3 "$here/test_server.py" -v
if [[ $ui == 1 && -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  run ui python3 "$here/ui_smoke.py"
fi
if [[ $ui == 1 && $(uname) == Darwin ]]; then
  run webkit python3 "$here/webkit_smoke.py"
  run app python3 "$here/app_smoke.py"
fi
run rust bash -c 'cargo test --workspace && cargo clippy --workspace --all-targets -- -D warnings'
[[ $bench == 1 ]] && run bench python3 "$here/bench.py" --out "$STUDIO_TEST_TMP/bench.json" $strict

# leftover servers from a crashed test would hold a port
ports=${STUDIO_TEST_PORTS:-7400-7499}
left=$(lsof -nP -iTCP:"$ports" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && /python/ {print $2}' | sort -u)

echo
echo "━━ summary  ($STUDIO_TEST_TMP)"
for i in "${!names[@]}"; do
  n=${names[$i]} rc=${results[$i]} t=${times[$i]}
  detail=""
  if [[ $n == cli || $n == server ]]; then
    detail=$(grep -E "^Ran [0-9]+ tests|^OK|^FAILED" "$logs/$n.log" | tr '\n' ' ')
    bad=$(grep -E "^(FAIL|ERROR): " "$logs/$n.log" | sed -E 's/^(FAIL|ERROR): ([a-z_0-9]+).*/\1 \2/' | tr '\n' ';')
    [[ -n $bad ]] && detail="$detail  [$bad]"
  elif [[ $n == ui || $n == webkit || $n == app ]]; then
    detail=$(grep -E "^SKIP" "$logs/$n.log" | head -1 | cut -c1-90)
    detail="$detail$(grep -E "^[0-9]+/[0-9]+ passed" "$logs/$n.log"; grep -E "^FAIL " "$logs/$n.log" | cut -c1-80 | tr '\n' ';')"
  elif [[ $n == rust ]]; then
    detail=$(awk '/^test result:/ {p+=$4; f+=$6} END {printf "%d passed, %d failed", p, f}' "$logs/$n.log")
    grep -qE "^error(\[|:)" "$logs/$n.log" && detail="$detail  [errors: see log]"
  elif [[ $n == bench ]]; then
    detail=$(grep -E "metric\(s\) over budget" "$logs/$n.log")
  fi
  printf '  %-8s %-4s %4ss  %s\n' "$n" "$([[ $rc == 0 ]] && echo ok || echo FAIL)" "$t" "$detail"
done
[[ -n $left ]] && echo "  warning: python still listening on $ports (pids: $left)"
[[ -f $STUDIO_TEST_TMP/bench.json ]] && echo "  bench json: $STUDIO_TEST_TMP/bench.json"
echo "  logs: $logs"
exit $fail
