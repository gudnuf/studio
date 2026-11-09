// Pure timeline operations. A timeline is {aspect, captions, clips:[{id, media, in, out, label?,
// moment? (the moment n it was added from), fit? ('fill' | 'fit'; absent = fitDefault)}]}.
// Every op returns a NEW timeline (snapshots make undo trivial and cheap).

import { round3 } from './util.js';

export const MIN_CLIP = 0.2;

export const newId = () => Math.random().toString(16).slice(2, 10).padEnd(8, '0');
export const dur = c => c.out - c.in;

const layoutCache = new WeakMap();
/** Story-time layout: starts[i], total. Cached per timeline object. */
export function layout(tl) {
  let L = layoutCache.get(tl);
  if (!L) {
    const starts = []; let t = 0;
    for (const c of tl.clips) { starts.push(t); t += dur(c); }
    L = { starts, total: t };
    layoutCache.set(tl, L);
  }
  return L;
}

/** Which clip is at story time t: {i, clip, start, local (seconds into clip), src (source time)} */
export function clipAt(tl, t) {
  const { starts, total } = layout(tl);
  if (!tl.clips.length) return null;
  t = Math.max(0, Math.min(t, total));
  let i = tl.clips.length - 1;
  for (let k = 0; k < starts.length; k++) {
    if (t < starts[k] + dur(tl.clips[k])) { i = k; break; }
  }
  const clip = tl.clips[i], start = starts[i];
  const local = Math.min(t - start, dur(clip));
  return { i, clip, start, local, src: clip.in + local };
}

export const indexOf = (tl, id) => tl.clips.findIndex(c => c.id === id);
const withClips = (tl, clips) => ({ ...tl, clips });

export function insert(tl, at, clip) {
  const clips = tl.clips.slice();
  const c = { id: newId(), ...clip, in: round3(clip.in), out: round3(clip.out) };
  clips.splice(at == null ? clips.length : at, 0, c);
  return [withClips(tl, clips), c];
}
export function remove(tl, id) {
  return withClips(tl, tl.clips.filter(c => c.id !== id));
}
export function move(tl, from, to) {
  const clips = tl.clips.slice();
  const [c] = clips.splice(from, 1);
  clips.splice(to > from ? to - 1 : to, 0, c);
  return withClips(tl, clips);
}
export function trim(tl, id, inn, out) {
  return withClips(tl, tl.clips.map(c => c.id === id ? { ...c, in: round3(inn), out: round3(out) } : c));
}
export function setLabel(tl, id, label) {
  return withClips(tl, tl.clips.map(c => {
    if (c.id !== id) return c;
    const n = { ...c };
    if (label) n.label = label; else delete n.label;
    return n;
  }));
}
/** Split clip at source time `at`. Returns [tl, rightId] or [tl, null] if too close to an edge. */
export function splitAt(tl, id, at) {
  const i = indexOf(tl, id);
  if (i < 0) return [tl, null];
  const c = tl.clips[i];
  if (at - c.in < MIN_CLIP || c.out - at < MIN_CLIP) return [tl, null];
  const right = { ...c, id: newId(), in: round3(at) };
  const clips = tl.clips.slice();
  clips.splice(i, 1, { ...c, out: round3(at) }, right);
  return [withClips(tl, clips), right.id];
}
export function duplicate(tl, id) {
  const i = indexOf(tl, id);
  if (i < 0) return [tl, null];
  const copy = { ...tl.clips[i], id: newId() };
  const clips = tl.clips.slice();
  clips.splice(i + 1, 0, copy);
  return [withClips(tl, clips), copy.id];
}
/** Remove the source range [a,b] from a clip, leaving up to two pieces. */
export function cutRange(tl, id, a, b) {
  const i = indexOf(tl, id);
  if (i < 0) return [tl, null];
  const c = tl.clips[i];
  a = Math.max(a, c.in); b = Math.min(b, c.out);
  if (b <= a) return [tl, null];
  const pieces = [];
  if (a - c.in >= MIN_CLIP) pieces.push({ ...c, out: round3(a) });
  if (c.out - b >= MIN_CLIP) pieces.push({ ...c, id: pieces.length ? newId() : c.id, in: round3(b) });
  const clips = tl.clips.slice();
  clips.splice(i, 1, ...pieces);
  return [withClips(tl, clips), pieces[0]?.id || pieces[1]?.id || null];
}
/** How a clip fills the frame: 'fill' | 'fit', or null to go back to the footage's default. */
export function setFit(tl, id, fit) {
  return withClips(tl, tl.clips.map(c => {
    if (c.id !== id) return c;
    const n = { ...c };
    if (fit) n.fit = fit; else delete n.fit;
    return n;
  }));
}
export function setFormat(tl, aspect) { return { ...tl, aspect }; }
export function setCaptions(tl, on) { return { ...tl, captions: !!on }; }

/** Canonical comparison (key order and float noise from the server don't count). */
const canon = tl => tl ? JSON.stringify([tl.aspect, !!tl.captions,
  (tl.clips || []).map(c => [c.id, c.media, round3(+c.in), round3(+c.out), c.label || '', c.moment ?? null, c.fit || null])]) : '';
export const same = (a, b) => canon(a) === canon(b);

/** Story time where clip `id` starts. */
export function startOf(tl, id) {
  const i = indexOf(tl, id);
  return i < 0 ? 0 : layout(tl).starts[i];
}

/** Three-way merge of two edits made from the same base (clips matched by id), for when the agent
 *  saved while the operator was editing. Each side's changes to clips the other side didn't touch
 *  survive: trims, labels, adds, deletes, and one side's reorder. Returns {tl, conflicts}, where
 *  conflicts counts clips both sides changed (theirs wins those; the caller offers Undo). */
export function merge3(base, mine, theirs) {
  const key = c => c ? JSON.stringify([c.media, c.in, c.out, c.label || null, c.fit || null, c.moment ?? null]) : null;
  const B = new Map(base.clips.map(c => [c.id, c])), M = new Map(mine.clips.map(c => [c.id, c])),
    T = new Map(theirs.clips.map(c => [c.id, c]));
  const changed = (side, id) => key(side.get(id)) !== key(B.get(id));
  let conflicts = 0;
  const pick = id => {
    const inB = B.has(id), inM = M.has(id), inT = T.has(id);
    if (!inB) return inM ? M.get(id) : T.get(id);                // added on one side
    if (!inM && !inT) return null;                                // both deleted
    if (!inM) { if (changed(T, id)) { conflicts++; return T.get(id); } return null; }  // I deleted it
    if (!inT) { if (changed(M, id)) { conflicts++; } return null; }                   // they deleted it
    const cm = changed(M, id), ct = changed(T, id);
    if (cm && ct && key(M.get(id)) !== key(T.get(id))) { conflicts++; return T.get(id); }
    return cm ? M.get(id) : T.get(id);
  };
  const order = tl => tl.clips.map(c => c.id).filter(id => B.has(id));
  const baseOrder = base.clips.map(c => c.id);
  const sameOrder = (a, b) => { const bb = b.filter(id => a.includes(id)); const aa = a.filter(id => bb.includes(id)); return aa.join() === bb.join(); };
  // skeleton = the side that reordered (theirs if both did), then weave in the other side's adds
  const theirsMoved = !sameOrder(order(theirs), baseOrder);
  const [skel, other] = theirsMoved ? [theirs, mine] : [mine, theirs];
  const ids = skel.clips.map(c => c.id);
  other.clips.forEach((c, i) => {
    if (B.has(c.id) || ids.includes(c.id)) return;
    const prev = other.clips.slice(0, i).reverse().find(p => ids.includes(p.id));
    ids.splice(prev ? ids.indexOf(prev.id) + 1 : 0, 0, c.id);
  });
  for (const id of baseOrder) if (!ids.includes(id)) ids.push(id);   // kept by the other side's change
  const clips = ids.map(pick).filter(Boolean);
  const meta = f => (mine[f] !== base[f] ? mine[f] : theirs[f]);
  return { tl: { ...theirs, aspect: meta('aspect'), captions: meta('captions'), clips }, conflicts };
}

/** What changed between two stories, by clip id — for "The agent removed …" toasts and the
 *  Activity feed. nameOf(clip) gives a human name for a clip. */
export function diffStory(a, b, nameOf = c => c.label || 'a clip') {
  const A = new Map((a?.clips || []).map((c, i) => [c.id, { c, i }]));
  const B = new Map((b?.clips || []).map((c, i) => [c.id, { c, i }]));
  const added = [], removed = [], trimmed = [], relabelled = [];
  for (const [id, x] of B) if (!A.has(id)) added.push(x.c);
  for (const [id, x] of A) if (!B.has(id)) removed.push({ ...x.c, _i: x.i });
  for (const [id, y] of B) {
    const x = A.get(id);
    if (!x) continue;
    if (Math.abs(x.c.in - y.c.in) > 1e-3 || Math.abs(x.c.out - y.c.out) > 1e-3) trimmed.push({ c: y.c, i: y.i, d: dur(y.c) - dur(x.c) });
    if ((x.c.label || '') !== (y.c.label || '')) relabelled.push(y.c);
  }
  // moved: relative order of clips present in both changed
  const keepA = (a?.clips || []).filter(c => B.has(c.id)).map(c => c.id);
  const keepB = (b?.clips || []).filter(c => A.has(c.id)).map(c => c.id);
  const moved = keepA.join() !== keepB.join();
  const parts = [];
  const q = c => `“${nameOf(c)}”`;
  const list = cs => cs.length === 1 ? q(cs[0]) : `${cs.length} clips`;
  if (removed.length) parts.push(`removed ${list(removed)}`);
  if (added.length) parts.push(`added ${list(added)}`);
  if (trimmed.length === 1) {
    const t = trimmed[0];
    parts.push(`trimmed clip ${t.i + 1} ${q(t.c)} (${t.d >= 0 ? '+' : '−'}${Math.abs(t.d).toFixed(1)}s)`);
  } else if (trimmed.length) parts.push(`trimmed ${trimmed.length} clips`);
  if (moved) parts.push('reordered the story');
  if (relabelled.length && !added.length) parts.push(`renamed ${list(relabelled)}`);
  if (a && b && a.aspect !== b.aspect) parts.push(`changed the format to ${b.aspect}`);
  if (a && b && !!a.captions !== !!b.captions) parts.push(`turned captions ${b.captions ? 'on' : 'off'}`);
  return { added, removed, trimmed, moved, relabelled, summary: parts.join(', ') };
}
