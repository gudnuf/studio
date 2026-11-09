// The one client-side store: server state, the working copy of the current story,
// selection, playhead, undo/redo, autosave and the focus beacon for Claude.

import * as api from './api.js';
import { layout, same, indexOf, clipAt, merge3 } from './ops.js';
import { throttle, debounce, clamp, fmtT } from './util.js';

const listeners = new Map();
export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
  return () => listeners.get(evt).delete(fn);
}
export function emit(evt, data) {
  for (const fn of listeners.get(evt) || []) {
    try { fn(data); } catch (e) { console.error(`[${evt}]`, e); }
  }
}

const EMPTY_TL = { aspect: '9:16', captions: true, clips: [] };

export const S = {
  server: null,          // last /api/state
  slug: localStorage.getItem('studio.project') || null,
  tl: EMPTY_TL,          // working copy of the story
  sel: null,             // selected clip id
  media: null,           // media id focused in the bin / inspector
  wordSel: null,         // {media, a, b} transcript word range
  playhead: 0,           // story time
  playing: false,
  saveState: 'saved',    // saved | pending | saving | error
  history: new Map(),    // slug -> {undo:[], redo:[]}
  gesture: null,         // snapshot taken at the start of a drag
  editSeq: 0, savedSeq: 0, saveCount: 0,
  lastRemoteActor: {},   // slug -> actor of the last timeline.saved we heard about
  rev: {},               // slug -> server revision our working copy is based on
  base: {},              // slug -> the server's story at that revision (merge base on conflict)
  findAll: null,         // {job, ids}: a "Find moments in all" run in progress (per-file toasts wait)
};
const bumpRev = (slug, rev) => { if (rev != null && !(S.rev[slug] >= rev)) S.rev[slug] = rev; };

/* ---------- derived ---------- */
export const project = () => S.server?.projects?.[S.slug] || null;
export const mediaOf = id => S.server?.media?.[id] || null;
/** Footage of this project: filed under it, or used in its story (e.g. added by Claude from elsewhere). */
export const projectMedia = () => {
  const used = new Set(S.tl.clips.map(c => c.media));
  return Object.values(S.server?.media || {}).filter(m => m.project === S.slug || used.has(m.id));
};
export const selClip = () => S.tl.clips.find(c => c.id === S.sel) || null;
export const selIndex = () => indexOf(S.tl, S.sel);
export const total = () => layout(S.tl).total;
export const dirty = () => S.gesture != null || S.editSeq !== S.savedSeq;
const hist = () => {
  if (!S.history.has(S.slug)) S.history.set(S.slug, { undo: [], redo: [] });
  return S.history.get(S.slug);
};
export const canUndo = () => hist().undo.length > 0;
export const canRedo = () => hist().redo.length > 0;
export const undoLabel = () => hist().undo.at(-1)?.label || '';
export const redoLabel = () => hist().redo.at(-1)?.label || '';

/* ---------- server state ---------- */
/** Snapshot of local edit/save progress; a state fetch begun before a change must not overwrite it. */
export const editMark = () => `${S.slug}:${S.editSeq}:${S.saveCount}:${S.gesture ? 1 : 0}`;

export function applyServer(st, { initial = false, mark = null } = {}) {
  S.server = st;
  const projects = st.projects || {};
  if (!S.slug || !projects[S.slug]) {
    const real = Object.values(projects).filter(p => p.slug !== 'inbox');
    const pick = real.sort((a, b) => String(b.created || '').localeCompare(String(a.created || '')))[0]
      || projects.inbox || Object.values(projects)[0];
    if (pick) setProject(pick.slug, { silent: true });
  }
  const p = project();
  if (p) {
    const remote = normalizeIncoming(p.timeline);
    const stale = mark != null && mark !== editMark();   // fetched before our latest edit/save landed
    if (!dirty() && !stale && same(remote, S.tl)) { bumpRev(S.slug, p.rev); S.base[S.slug] = remote; }
    if (!dirty() && !stale && !same(remote, S.tl)) {
      bumpRev(S.slug, p.rev);
      S.base[S.slug] = remote;
      const actor = S.lastRemoteActor[S.slug];
      if (!initial && S.tl.clips.length + remote.clips.length > 0) {
        pushUndo(S.tl, actor === 'claude' ? "Claude's edit" : 'Outside edit');
      }
      const prev = S.tl;
      S.tl = remote;
      fixSelection();
      if (S.playhead > total()) { S.playhead = total(); emit('playhead', { from: 'remote' }); }
      emit('timeline', { remote: !initial, actor, prev });
      emit('history');
    }
  }
  emit('server', st);
}

function normalizeIncoming(tl) {
  if (!tl) return { ...EMPTY_TL };
  return {
    aspect: tl.aspect || '9:16',
    captions: tl.captions !== false,
    clips: (tl.clips || []).map(c => ({ ...c, in: +c.in, out: +c.out })),
  };
}

export function setProject(slug, { silent = false } = {}) {
  if (slug === S.slug && !silent && S.server) return;
  if (dirty()) saveNow();
  S.slug = slug;
  localStorage.setItem('studio.project', slug);
  const p = S.server?.projects?.[slug];
  S.tl = normalizeIncoming(p?.timeline);
  S.rev[slug] = p?.rev ?? 0;
  S.base[slug] = S.tl;
  S.sel = null; S.wordSel = null;
  S.media = null;
  S.playhead = 0;
  S.editSeq = S.savedSeq;
  emit('project', slug);
  emit('timeline', {});
  emit('select');
  emit('playhead', { from: 'project' });
  emit('history');
  beacon();
}

/* ---------- edits, undo/redo ---------- */
function pushUndo(tl, label) {
  const h = hist();
  h.undo.push({ tl, label, sel: S.sel });
  if (h.undo.length > 200) h.undo.shift();
  h.redo.length = 0;
}

/** Commit a new story. Everything the user does goes through here (or a gesture). */
export function commit(tl, label, { select } = {}) {
  if (same(tl, S.tl)) return;
  pushUndo(S.tl, label);
  S.tl = tl;
  if (select !== undefined) S.sel = select;
  afterEdit();
}

export function undo() {
  const h = hist();
  const e = h.undo.pop();
  if (!e) return;
  h.redo.push({ tl: S.tl, label: e.label, sel: S.sel });
  S.tl = e.tl; S.sel = e.sel;
  afterEdit();
  return e.label;
}
export function redo() {
  const h = hist();
  const e = h.redo.pop();
  if (!e) return;
  h.undo.push({ tl: S.tl, label: e.label, sel: S.sel });
  S.tl = e.tl; S.sel = e.sel;
  afterEdit();
  return e.label;
}

function afterEdit() {
  S.editSeq++;
  fixSelection();
  S.playhead = clamp(S.playhead, 0, total());
  emit('timeline', {});
  emit('select');
  emit('history');
  emit('playhead', { from: 'edit' });
  scheduleSave();
  beacon();
}

/* Gestures (drag / trim): preview freely, then one undo entry at the end. */
export function gestureBegin() { if (!S.gesture) S.gesture = S.tl; }
export function gesturePreview(tl) {
  S.tl = tl;
  emit('timeline', { preview: true });
}
export function gestureEnd(label) {
  const before = S.gesture;
  S.gesture = null;
  if (!before) return;
  if (same(before, S.tl)) { S.tl = before; emit('timeline', {}); return; }
  const after = S.tl;
  S.tl = before;
  commit(after, label);
}
export function gestureCancel() {
  if (!S.gesture) return;
  S.tl = S.gesture; S.gesture = null;
  emit('timeline', {});
}

function fixSelection() {
  if (S.sel && indexOf(S.tl, S.sel) < 0) S.sel = null;
}

/* ---------- autosave ---------- */
let inflight = null;
async function save() {
  if (S.gesture) { scheduleSave(); return; }
  if (inflight) { await inflight.catch(() => {}); }
  const seq = S.editSeq, slug = S.slug, body = S.tl;
  if (seq === S.savedSeq) return;
  setSave('saving');
  inflight = api.putTimeline(slug, body, S.rev[slug] ?? 0);   // 0: nobody has saved this story yet
  try {
    const { timeline: res, rev } = await inflight;
    if (rev != null) S.rev[slug] = rev;
    if (S.slug === slug && S.editSeq === seq) {
      S.savedSeq = seq;
      S.saveCount++;
      const norm = normalizeIncoming(res);
      S.base[slug] = norm;
      if (res && !same(norm, S.tl)) {   // server clamped something; adopt its version quietly
        S.tl = norm; fixSelection();
        emit('timeline', { normalized: true });
      }
      setSave('saved');
    } else if (S.slug !== slug) {
      setSave('saved');
    }
  } catch (e) {
    if (e.conflict && S.slug === slug) {
      // Someone (usually Claude) saved while we were editing. Merge both edits (clips by id);
      // where both changed the same clip theirs wins and ours stays one Undo away.
      const c = e.conflict;
      S.rev[slug] = c.rev;
      S.lastRemoteActor[slug] = c.actor || 'cli';
      const remote = normalizeIncoming(c.timeline);
      const base = S.base[slug];
      const { tl: merged, conflicts } = base ? merge3(base, S.tl, remote) : { tl: remote, conflicts: 1 };
      S.base[slug] = remote;
      pushUndo(S.tl, conflicts ? 'your version' : 'merge with Claude’s edit');
      const prev = S.tl;
      S.tl = merged;
      if (same(merged, remote)) S.savedSeq = S.editSeq;
      else { S.editSeq++; }
      fixSelection();
      if (S.editSeq !== S.savedSeq) scheduleSave(); else setSave('saved');
      emit('timeline', { remote: true, actor: c.actor, conflict: true, merged: !conflicts, conflicts, prev });
      emit('history');
    } else {
      console.warn('save failed', e);
      setSave('error');
      setTimeout(scheduleSave, 2000);
    }
  } finally { inflight = null; }
}
const saveDebounced = debounce(save, 400);
export function scheduleSave() { setSave('pending'); saveDebounced(); }
export function saveNow() { saveDebounced.cancel(); return save(); }
function setSave(s) { S.saveState = s; emit('save', s); }

/* ---------- selection & playhead ---------- */
export function select(id, { seek = false } = {}) {
  S.sel = id;
  const c = selClip();
  if (c) S.media = c.media;
  if (c && seek) {
    const i = indexOf(S.tl, id);
    setPlayhead(layout(S.tl).starts[i] + 0.0001, { from: 'select' });
  }
  S.wordSel = null;
  emit('select');
  emit('media');
  beacon();
}
export function focusMedia(id) {
  S.media = id;
  S.wordSel = null;
  emit('media');
  beacon();
}
export function setPlayhead(t, opts = {}) {
  S.playhead = clamp(t, 0, total());
  emit('playhead', opts);
  beaconSlow();
}
export function setWordSel(ws) { S.wordSel = ws; beacon(); }

/* ---------- focus beacon: so `studio focus` can resolve "this clip" ---------- */
export function focusPayload() {
  const c = selClip(), i = selIndex(), m = c ? mediaOf(c.media) : mediaOf(S.media);
  const at = clipAt(S.tl, S.playhead);
  return {
    project: S.slug,
    rev: S.rev[S.slug] ?? null,            // pass as --base to timeline verbs
    playhead: +S.playhead.toFixed(2),
    playhead_hms: fmtT(S.playhead),
    // index is 0-based; number is what the UI shows ("Clip 3 of 4") and what the CLI takes
    under_playhead: at ? { index: at.i, number: at.i + 1, id: at.clip.id, media: at.clip.media, source_time: +at.src.toFixed(2) } : null,
    selection: c ? { index: i, number: i + 1, of: S.tl.clips.length, id: c.id, media: c.media, name: m?.name, in: c.in, out: c.out, label: c.label || null, moment: c.moment ?? null } : null,
    media: m ? { id: m.id, name: m.name } : null,
    transcript_selection: S.wordSel || null,
    aspect: S.tl.aspect, captions: S.tl.captions,
    cli: cliFor(),
    ts: new Date().toISOString(),
  };
}
const beacon = throttle(() => S.slug && api.postFocus(focusPayload()), 300);
const beaconSlow = throttle(() => S.slug && !S.playing && api.postFocus(focusPayload()), 800);
export const sendFocus = beacon;

export function cliFor() {
  const c = selClip();
  if (!S.slug) return 'studio project ls';
  if (!c) return `studio timeline show ${S.slug}`;
  return `studio timeline trim ${S.slug} ${c.id} ${(+c.in).toFixed(2).replace(/\.?0+$/, '') || 0} ${(+c.out).toFixed(2).replace(/\.?0+$/, '')}`;
}
