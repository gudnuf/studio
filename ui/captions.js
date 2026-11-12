// THE caption rule on the UI side. studio.py caption_chunks() is the same rule for the export,
// and test/ui_smoke.py checks the two agree, so the preview shows what the export burns in.
//
// The words of [start, end) in chunks of at most 3, closing a chunk early after a word ending
// in . ? ! or , and at a segment boundary. Each chunk shows from its first word until the next
// chunk starts (the last one until its last word ends), in clip time. A word counts if it
// starts inside the clip even when whisper gave it no length. No chunk shows for under CAP_MIN:
// a word of the sentence before or after that the clip's edge cuts through is dropped, and any
// other short chunk joins the one before it (when it ends a phrase, or is the last) or the one
// after. (The export also holds each chunk to the speech the VAD hears, studio.py caption_gate:
// at most a few tenths of a second later or shorter than this preview, and now and then two of
// these chunks shown as one, when the voice comes too late for the first to be read alone.)

export const CAP_WORDS = 3;
export const CAP_MIN = 0.4;   // no caption shows for less (QUALITY.md `caption stray`)
const BREAK = /[.?!,]["'”’)\]]*$/;
const STOP = /[.?!]["'”’)\]]*$/;    // the end of a sentence

/** Index of the last element of sorted `arr` that is <= x, or -1. */
function lastLE(arr, x) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans;
}

export function captionChunks(words, segments, start, end, pace = 1) {
  const least = CAP_MIN * pace;   // CAP_MIN is on the export's clock; these times are source seconds
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
  const its = [];
  groups.forEach((g, i) => {
    const a = Math.max(g[0].start, start) - start;
    let b = (i + 1 < groups.length ? groups[i + 1][0].start : Math.max(g.at(-1).end, g.at(-1).start + least)) - start;
    b = Math.min(b, end - start);
    if (b - a > 0.01) its.push({ g, a, b });
  });
  const short = x => x.b - x.a < least - 1e-9;
  // every word of it began before the clip did: the tail of the sentence before
  if (its.length && short(its[0]) && its[0].g.every(w => w.start < start)) its.shift();
  // every word ends after the clip does, or it follows the end of a sentence and the clip can't
  // show it for CAP_MIN: the first words of the sentence after
  if (its.length && short(its.at(-1)) && (its.at(-1).g.every(w => w.end > end)
      || (its.length > 1 && STOP.test(its.at(-2).g.at(-1).w)))) its.pop();
  let i = 0;
  while (i < its.length) {
    const x = its[i];
    if (!short(x) || its.length === 1) i++;
    else if (i > 0 && (BREAK.test(x.g.at(-1).w) || i + 1 === its.length)) {
      its[i - 1].g = its[i - 1].g.concat(x.g); its[i - 1].b = x.b; its.splice(i, 1);
    } else {
      its[i + 1].g = x.g.concat(its[i + 1].g); its[i + 1].a = x.a; its.splice(i, 1);
    }
  }
  return its.map(x => ({ a: r3(x.a), b: r3(x.b), text: x.g.map(w => w.w).join(' ') }));
}
