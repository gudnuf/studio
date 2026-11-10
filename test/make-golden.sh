#!/usr/bin/env bash
# Build the golden fold fixtures (docs/events.md §5; plan task 0.2) in test/golden/fold/:
#
#   real.events.jsonl    the operator's log, scrubbed (test/scrub.py): keyed (HMAC, secret outside the
#                        repo), every free-text word/number/name a 6-letter token, ids and hashes
#                        HMAC'd consistently, timestamps, numbers and enum values kept
#   real.state.json      `studio state --json` of it
#   seeded.events.jsonl  test/seed.py --v2: a deterministic log through studio.py's own verbs
#   seeded.state.json    `studio state --json` of it
#
# Absolute library paths are written as the literal $STUDIO_HOME, in logs and states alike.
#
#   test/make-golden.sh                  refold both logs and regenerate the seeded log
#   test/make-golden.sh --real CLONE     also refresh real.events.jsonl from CLONE/events.jsonl, a
#                                        clone of the library (/bin/cp -c -R ~/Studio CLONE); never
#                                        ~/Studio itself. Its transcripts gate the scrub. Needs the
#                                        scrub key (STUDIO_SCRUB_KEY or ~/.config/studio/scrub.key):
#                                        without it there is no --real, and only the key's holder
#                                        can regenerate the same real.* bytes.
#   test/make-golden.sh --check          build into a temp dir and diff against the committed files
#                                        (exit 1 on any difference)
#
# Same inputs, same bytes: running it twice changes nothing.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
  exec nix develop "$root" --quiet -c bash "$here/make-golden.sh" "$@" 2> >(grep -v "Git tree .* is dirty" >&2)
fi

real="" check=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --real) real=$2; shift ;;
    --check) check=1 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done

golden="$root/test/golden/fold"
tmp=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/studio-golden.XXXXXX")" && pwd -P)   # /var → /private/var
trap 'rm -rf "$tmp"' EXIT
out=$golden
[[ $check == 1 ]] && { out="$tmp/out"; mkdir -p "$out"; cp "$golden/real.events.jsonl" "$out/"; }
mkdir -p "$out"

unset CLAUDECODE CODEX_THREAD_ID CODEX_SANDBOX GOOSE_TERMINAL STUDIO_ACTOR STUDIO_DEVICE

# sub SRC DST FROM TO: DST = SRC's text with every FROM replaced by TO
sub() { python3 - "$@" <<'EOF'
import sys
src, dst, frm, to = sys.argv[1:5]
data = open(src, encoding="utf-8").read()
open(dst, "w", encoding="utf-8").write(data.replace(frm, to))
EOF
}

# fold LOG STATE: STATE = `studio state --json` of LOG in a fresh home
fold() {
  local home; home=$(mktemp -d "$tmp/home.XXXXXX")
  sub "$1" "$home/events.jsonl" '$STUDIO_HOME' "$home"
  STUDIO_HOME=$home python3 "$root/studio.py" state --json > "$home/state.json"
  sub "$home/state.json" "$2" "$home" '$STUDIO_HOME'
}

if [[ -n $real ]]; then
  clone=$(cd "$real" && pwd -P)
  studio=$(cd "$HOME/Studio" 2>/dev/null && pwd -P || true)
  if [[ -n $studio && $clone == "$studio" ]]; then
    echo "make-golden: --real must be a clone of the library, never ~/Studio itself" >&2; exit 2
  fi
  # the log's own absolute paths (the library's, at the time) are scrubbed like any text
  python3 "$here/scrub.py" "$clone/events.jsonl" "$out/real.events.jsonl" --forbid-from "$clone"
fi
fold "$out/real.events.jsonl" "$out/real.state.json"

seedhome="$tmp/seed"
python3 "$here/seed.py" --v2 "$seedhome" > /dev/null
sub "$seedhome/events.jsonl" "$out/seeded.events.jsonl" "$seedhome" '$STUDIO_HOME'
fold "$out/seeded.events.jsonl" "$out/seeded.state.json"

if [[ $check == 1 ]]; then
  rc=0
  for f in real.events.jsonl real.state.json seeded.events.jsonl seeded.state.json; do
    if ! cmp -s "$out/$f" "$golden/$f"; then echo "make-golden --check: $f differs" >&2; rc=1; fi
  done
  [[ $rc == 0 ]] && echo "make-golden --check: test/golden/fold matches"
  exit $rc
fi
ls -l "$golden"
