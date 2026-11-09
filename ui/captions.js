// THE caption rule on the UI side. studio.py caption_chunks() is the same rule for the export,
// and test/ui_smoke.py checks the two agree, so the preview shows what the export burns in.
//
// The words of [start, end) in chunks of at most 3, closing a chunk early after a word ending
// in . ? ! or , and at a segment boundary. Each chunk shows from its first word until the next
// chunk starts (the last one until its last word ends), in clip time. A word counts if it
// starts inside the clip even when whisper gave it no length.

export const CAP_WORDS = 3;
const BREAK = /[.?!,]["'”’)\]]*$/;

/** Index of the last element of sorted `arr` that is <= x, or -1. */
function lastLE(arr, x) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans;
}

export function captionChunks(words, segments, start, end) {
  const ws = (words || []).filter(x => x.start < end && (x.end > start || x.start >= start));
  const starts = (segments || []).map(s => s.start).sort((a, b) => a - b);
  const seg = x => lastLE(starts, x.start + 1e-3);
  const groups = [];
  let cur = [];
  ws.forEach((x, k) => {
    cur.push(x);
    const nxt = ws[k + 1];
    if (!nxt || cur.length >= CAP_WORDS || BREAK.test(x.w) || seg(nxt) !== seg(x)) { groups.push(cur); cur = []; }
  });
  const r3 = v => Math.round(v * 1000) / 1000;
  const out = [];
  groups.forEach((g, i) => {
    const a = Math.max(g[0].start, start) - start;
    let b = (i + 1 < groups.length ? groups[i + 1][0].start : Math.max(g.at(-1).end, g.at(-1).start + 0.3)) - start;
    b = Math.min(b, end - start);
    if (b - a > 0.01) out.push({ a: r3(a), b: r3(b), text: g.map(x => x.w).join(' ') });
  });
  return out;
}
