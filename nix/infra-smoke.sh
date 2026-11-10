# `nix run .#infra-smoke`: prove a running relay + Blossom server (dev-infra or a deploy) do what
# the studio needs. Throwaway key each run; writes only to a temp dir.
#   1. a NIP-44 self-encrypted event published by one client is read back by another and decrypts
#   2. BUD-06 preflight: HEAD /upload for a 64 MiB blob, signed -> 200; unsigned -> 401
#   3. a blob uploaded with a signed (kind 24242) auth event downloads byte-identical, ranges work
#   4. an unsigned upload is refused
# Targets: STUDIO_RELAY / STUDIO_BLOSSOM, else the dev-infra defaults. STUDIO_SMOKE_SEC: the
# secret key to sign with (needed when the servers have an allowlist); else a throwaway key.
relay="${STUDIO_RELAY:-ws://127.0.0.1:7347}"
blossom="${STUDIO_BLOSSOM:-http://127.0.0.1:7348}"
blossom="${blossom%/}"
exec </dev/null   # nak reads events from a non-tty stdin; never let it wait on ours
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail() {
  echo "FAIL: $*" >&2
  case "$*" in *"not allowed"*|*"not authorized"*|*401*|*403*)
    [ -z "${STUDIO_SMOKE_SEC:-}" ] && echo "  (servers with an allowlist? pass an allowed key as STUDIO_SMOKE_SEC)" >&2 ;; esac
  exit 1
}
ok() { echo "ok   $*"; }

sk="${STUDIO_SMOKE_SEC:-$(nak key generate)}"   # an allowlisted key when the servers have one
pk="$(nak key public "$sk")"
echo "relay $relay  blossom $blossom  key ${pk:0:12}…"

# 1. relay round trip, NIP-44 v2 to self (the shape of a library log line)
plain="studio smoke $(date +%s) $RANDOM"
cipher="$(nak encrypt --sec "$sk" -p "$pk" "$plain")"
id="$(nak event --sec "$sk" -k 1 -c "$cipher" -t L=smoke "$relay" </dev/null 2>"$tmp/pub.err" | jq -r .id)" \
  || fail "publish: $(cat "$tmp/pub.err")"
[ -n "$id" ] && [ "$id" != null ] || fail "publish: no id ($(cat "$tmp/pub.err"))"
grep -qi "fail\|error\|rejected" "$tmp/pub.err" && fail "publish: $(cat "$tmp/pub.err")"
back="$(nak req -i "$id" "$relay" 2>/dev/null | jq -r .content || true)"
[ "$back" = "$cipher" ] || fail "read back: got '${back:0:40}'"
[ "$(nak decrypt --sec "$sk" -p "$pk" "$back")" = "$plain" ] || fail "decrypt"
ok "relay: event ${id:0:12}… published, read back by a second client, NIP-44 decrypts"

auth() { # auth <verb> [sha256] -> Authorization header value (BUD-11, kind 24242)
  local tags=(-t "t=$1" -t "expiration=$(( $(date +%s) + 300 ))")
  [ -n "${2:-}" ] && tags+=(-t "x=$2")
  echo "Nostr $(nak event --sec "$sk" -k 24242 -c "studio $1" "${tags[@]}" </dev/null | base64 | tr -d '\n')"
}
# never let a refused connection end the script silently under `set -e`: 000 then reaches a FAIL
code() { curl -s -o /dev/null -w '%{http_code}' "$@" || true; }

# 2. BUD-06 preflight at the chunk size
big_sha="$(printf '%064d' 0 | tr 0 a)"
c="$(code -I "$blossom/upload" -H "X-SHA-256: $big_sha" -H "X-Content-Length: 67108864" \
  -H "X-Content-Type: application/octet-stream" -H "Authorization: $(auth upload "$big_sha")")"
[ "$c" = 200 ] || fail "HEAD /upload 64 MiB signed: $c"
c="$(code -I "$blossom/upload" -H "X-SHA-256: $big_sha" -H "X-Content-Length: 67108864" \
  -H "X-Content-Type: application/octet-stream")"
[ "$c" = 401 ] || fail "HEAD /upload unsigned: $c (want 401)"
ok "blossom: HEAD /upload X-Content-Length 67108864 signed=200 unsigned=401"

# 3. signed upload, byte-identical download, a range
head -c 1048576 /dev/urandom > "$tmp/blob"
sha="$(sha256sum "$tmp/blob" | cut -d' ' -f1)"
c="$(curl -s -o "$tmp/desc" -w '%{http_code}' -X PUT "$blossom/upload" --data-binary @"$tmp/blob" \
  -H "Content-Type: application/octet-stream" -H "Authorization: $(auth upload "$sha")" || true)"
[ "$c" = 200 ] || [ "$c" = 201 ] || fail "PUT /upload: $c $(cat "$tmp/desc")"
[ "$(jq -r .sha256 "$tmp/desc" 2>/dev/null || true)" = "$sha" ] || fail "descriptor sha256: $(cat "$tmp/desc")"
curl -sf "$blossom/$sha" -o "$tmp/down" || fail "GET /$sha"
cmp -s "$tmp/blob" "$tmp/down" || fail "download differs"
c="$(curl -s -o "$tmp/range" -w '%{http_code}' -H "Range: bytes=1000-1999" "$blossom/$sha" || true)"
if [ "$c" != 206 ] || ! cmp -s "$tmp/range" <(tail -c +1001 "$tmp/blob" | head -c 1000); then fail "range GET: $c"; fi
ok "blossom: 1 MiB blob ${sha:0:12}… uploaded with a signed auth event, downloaded identical, range 206"

# 4. unsigned upload refused
head -c 1024 /dev/urandom > "$tmp/small"
c="$(code -X PUT "$blossom/upload" --data-binary @"$tmp/small" -H "Content-Type: application/octet-stream")"
[ "$c" = 401 ] || fail "unsigned PUT: $c (want 401)"
ok "blossom: unsigned upload refused (401)"

# tidy: delete the test blob (signed)
c="$(code -X DELETE "$blossom/$sha" -H "Authorization: $(auth delete "$sha")")"
case "$c" in 200|204) ;; *) fail "DELETE (tidy): $c" ;; esac
echo "smoke: all good"
