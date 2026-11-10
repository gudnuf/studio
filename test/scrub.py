#!/usr/bin/env python3
"""Scrub a studio event log for use as a public golden fixture (docs/events.md, plan task 0.2).

    STUDIO_SCRUB_KEY=… python3 test/scrub.py IN.events.jsonl OUT.events.jsonl [--forbid-from LIBRARY]

The repo is public, so the scrub must not be reversible by anyone holding this file:

- **Keyed.** Every replacement is HMAC-SHA256 under a secret that is never in the repo: the
  `STUDIO_SCRUB_KEY` environment variable, else the file `~/.config/studio/scrub.key` (create it
  once: `umask 077; mkdir -p ~/.config/studio; openssl rand -hex 32 > ~/.config/studio/scrub.key`).
  No key, no scrub. Without the key a placeholder says nothing about its word; with the same key
  a regeneration is byte-identical.
- **No side channels.** Every alphanumeric run in free text (words, numbers, file names, the
  pieces of slugs and paths) becomes one 6-letter lowercase consonant token: no length, no case,
  no digits carried over. Punctuation and spacing stay, so a path still looks like a path.
- **Field-level keep.** A string is kept verbatim only when it is a whole timestamp, or an enum
  value from ENUMS under its own key (rule names, steps, actors, kinds, statuses, …). Lists and
  objects are always walked: nothing is kept because of the key above it.
- **Hashes and ids** (media ids, clip/note/exception ids, sha256s, fingerprints) are HMAC'd to hex
  of the same length, one map for the whole log, prefixes kept consistent (a media id stays the
  first 12 hex of its file's sha256), so the log still folds.

The mapping is consistent across the whole log, so the scrubbed log folds into a state of the
same shape. Gates (fail = no output): every alphabetic token left in the output that has a vowel
must be an allowed enum or schema word; with --forbid-from LIBRARY (a clone, never ~/Studio), no
word of its transcripts may appear anywhere a scrubbed value sits.
"""
import argparse, hashlib, hmac, json, os, re, sys
from pathlib import Path

KEY_FILE = Path.home() / ".config" / "studio" / "scrub.key"

# Enum values kept verbatim, and only under these keys.
ENUMS = {
    "kind": {"upload", "steps", "render", "check", "share", "import", "sound", "picture", "card",
             "onscreen", "version", "moment", "draft", "other"},
    "step": {"waiting", "proxy", "speech", "transcribe", "keyframes", "moments", "cut", "draft", "render"},
    "steps": {"proxy", "speech", "transcribe", "keyframes", "moments", "cut", "draft"},
    "status": {"pass", "fail", "warn", "skip", "excepted", "running", "done", "failed"},
    "rule": set(), "failed": set(),            # filled from RULES below
    "actor": {"operator", "agent", "cli", "claude", "codex", "goose"},
    "rev_actor": set(), "by": set(), "granted_by": set(),   # = actor
    "aspect": {"9:16", "16:9", "1:1", "4:5"},
    "voice": {"natural", "off"},
    "fit": {"fill", "fit"},
    "encoder": {"videotoolbox", "libx264", "h264_videotoolbox", "hevc_videotoolbox"},
    "concat": {"copy", "reencode"},
    "action": {"pick", "keep", "rewrite", "reject"},
    "brand": {"jefe"},                          # brands/jefe.md is in the repo
    "verdict": {"ready", "failed", "unchecked"},
    "state": {"ready", "failed", "unchecked"},
    "field": {"short", "x", "linkedin", "title", "newsletter"},
    "language": {"en"},
    "provider": {"claude", "codex", "goose"},
}
RULES = {"cuts", "shortest", "first word", "pauses", "length", "loudness", "picture", "captions",
         "ending", "flash", "stray", "pop", "jump", "fragment", "click", "held", "soft"}
ENUMS["rule"] = ENUMS["failed"] = RULES
ENUMS["rev_actor"] = ENUMS["by"] = ENUMS["granted_by"] = ENUMS["actor"]

# Keys whose string values (or list members) are ids or hashes: HMAC'd hex.
HEX_KEYS = {"id", "media", "media_id", "clip", "sha256", "fp", "reel", "re", "ids", "excepted",
            "round", "labels", "shown", "chosen", "files", "timeline_sha", "prompt", "step_sha256",
            "blob", "chunks", "clips"}
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:\d\d|Z)?$")
JOB_RE = re.compile(r"^\d{10}-\d+$")             # server job ids: unix seconds + a counter
HEX_RE = re.compile(r"^[0-9a-f]{4,64}$")
TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
CONS = "bcdfghjklmnpqrstvwxz"
TOKEN_LEN = 6


def load_key():
    k = os.environ.get("STUDIO_SCRUB_KEY", "").strip()
    if not k and KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
    if len(k) < 32:
        sys.exit(f"scrub: no key. Set STUDIO_SCRUB_KEY or create {KEY_FILE} (32+ chars, outside the repo); "
                 "see the docstring")
    return k.encode()


class Scrubber:
    def __init__(self, key, forbid=()):
        self.key = key
        self.words, self.used = {}, set()
        self.hexmap = {}
        self.forbid = {w.lower() for w in forbid}
        self.full = set()                          # 64-hex values seen under id keys
        self.known = set()                         # every hex value seen under an id key

    def mac(self, kind, s):
        return hmac.new(self.key, f"{kind}\0{s}".encode(), hashlib.sha256).digest()

    def word(self, w):
        w = w.lower()
        if w not in self.words:
            n = 0
            while True:
                h = self.mac(f"word{n}", w)
                cand = "".join(CONS[b % len(CONS)] for b in h[:TOKEN_LEN])
                if cand not in self.used and cand not in self.forbid:
                    break
                n += 1
            self.words[w] = cand
            self.used.add(cand)
        return self.words[w]

    def hexid(self, h):
        if h not in self.hexmap:
            full = [f for f in self.full if f.startswith(h)] if len(h) < 64 else []
            if len(full) == 1:                    # a prefix of a known sha256: the same prefix of its image
                self.hexmap[h] = self.hexid(full[0])[:len(h)]
            else:
                self.hexmap[h] = (self.mac("hex", h).hex() * 2)[:len(h)]
        return self.hexmap[h]

    def text(self, s):
        """Free text: every alphanumeric run becomes a token (a known id stays a mapped id)."""
        return TOKEN_RE.sub(lambda m: self.hexid(m.group(0)) if m.group(0) in self.known
                            else self.word(m.group(0)), s)

    def leaf(self, s, key):
        if TS_RE.match(s):
            return s
        if key in ENUMS and s in ENUMS[key]:
            return s
        if key in HEX_KEYS and s in self.known:
            return self.hexid(s)
        if key == "job" and JOB_RE.match(s):
            return s
        return self.text(s)

    def value(self, v, key):
        if isinstance(v, str):
            return self.leaf(v, key)
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if key == "projects" or not re.fullmatch(r"[a-z_]+|[A-F]", k):
                    nk = self.hexid(k) if k in self.known else self.text(k)   # ids, slugs as keys
                else:
                    nk = k
                out[nk] = self.value(x, k if not (key == "labels" or key == "options") else key)
            return out
        return v

    def collect(self, v, key):
        # (the event's own "media" is collected by main)
        """Pass 1: every hex value under an id key, so prefixes and free-text mentions map alike."""
        if isinstance(v, str):
            if key in HEX_KEYS and HEX_RE.match(v):
                self.known.add(v)
                if len(v) == 64:
                    self.full.add(v)
        elif isinstance(v, list):
            for x in v:
                self.collect(x, key)
        elif isinstance(v, dict):
            for k, x in v.items():
                if key in ("options", "checks", "exceptions") and HEX_RE.match(k):
                    self.known.add(k)
                    if len(k) == 64:
                        self.full.add(k)
                self.collect(x, k if key != "labels" else key)

    def event(self, ev):
        out = {}
        for k, v in ev.items():
            if k == "data":
                out[k] = {dk: self.value(dv, dk) for dk, dv in v.items()}
            elif k == "media" and isinstance(v, str):
                out[k] = self.hexid(v)
            else:
                out[k] = v                         # id (UUIDv7: time + random), ts, type, device, imported
        return out


SCHEMA_WORDS = None


def allowed_words():
    """Words that may appear verbatim: enum values, event types, data/state keys."""
    global SCHEMA_WORDS
    if SCHEMA_WORDS is None:
        w = set()
        for vals in ENUMS.values():
            for v in vals:
                w.update(x.lower() for x in TOKEN_RE.findall(v))
        SCHEMA_WORDS = w
    return SCHEMA_WORDS


def vowel_gate(sc, events):
    """Every alphabetic token with a vowel must be an allowed word, a dict key, or a mapped id."""
    allow = allowed_words()
    mapped = set(sc.hexmap.values())
    bad = {}
    def walk(v, path):
        if isinstance(v, str):
            if TS_RE.match(v):
                return
            for t in TOKEN_RE.findall(v):
                if re.search(r"[aeiouAEIOU]", t) and t not in mapped and t.lower() not in allow:
                    bad.setdefault(t, path)
        elif isinstance(v, list):
            for x in v:
                walk(x, path)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
    for ev in events:
        walk(ev.get("data"), ev.get("type"))
        if isinstance(ev.get("media"), str):
            walk(ev["media"], "media")
    return bad


def transcript_words(lib):
    lib = Path(lib).resolve()
    if lib == (Path.home() / "Studio").resolve():
        sys.exit("scrub: --forbid-from must be a clone of the library, never ~/Studio itself")
    words = set()
    for t in sorted(lib.glob("media/*/transcript.txt")):
        words |= {w.lower() for w in re.findall(r"[A-Za-z]+", t.read_text(errors="replace"))}
    return words


def transcript_leaks(sc, events, words):
    """Transcript words standing anywhere except an allowed enum position or inside a mapped id."""
    allow, mapped = allowed_words(), set(sc.hexmap.values())
    hits = set()
    def walk(v):
        if isinstance(v, str):
            if not TS_RE.match(v):
                hits.update(w for t in TOKEN_RE.findall(v) if t not in mapped
                            for w in (x.lower() for x in re.findall(r"[A-Za-z]+", t))
                            if w in words and w not in allow)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    for ev in events:
        walk(ev.get("data"))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--forbid-from", metavar="LIBRARY")
    a = ap.parse_args()
    key = load_key()
    words = transcript_words(a.forbid_from) if a.forbid_from else set()
    evs = []
    for i, line in enumerate(Path(a.src).read_text(errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            evs.append(json.loads(line))
        except ValueError:
            print(f"scrub: dropping unreadable line {i + 1}", file=sys.stderr)
    sc = Scrubber(key, words)
    for ev in evs:
        sc.collect(ev.get("data"), None)
        if isinstance(ev.get("media"), str):
            sc.known.add(ev["media"])
    out = [sc.event(ev) for ev in evs]
    bad = vowel_gate(sc, out)
    if bad:
        sys.exit("scrub: words survived (token: where): " +
                 "; ".join(f"{t}: {p}" for t, p in sorted(bad.items())[:30]))
    if words:
        leak = transcript_leaks(sc, out, words)
        if leak:
            sys.exit(f"scrub: transcript words survived: {sorted(leak)[:30]}")
    Path(a.out).write_text("".join(json.dumps(ev, ensure_ascii=False) + "\n" for ev in out))
    print(f"scrub: {len(out)} events, {len(sc.words)} words and {len(sc.hexmap)} ids replaced"
          + (f", checked against {len(words)} transcript words" if words else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
