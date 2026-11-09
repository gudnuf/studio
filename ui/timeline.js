// The Story timeline: ruler, one track of clip blocks, playhead layer.
// Performance rules: clip nodes are keyed and reused; positions are transforms; DOM writes are
// batched into one animation frame; the playhead is its own layer and never triggers a re-render;
// filmstrips only rebuild when a clip's range or the zoom changes (not on every drag frame).

import { S, on, emit, commit, select, setPlayhead, gestureBegin, gesturePreview, gestureEnd,
  gestureCancel, mediaOf, total } from './store.js';
import { layout, dur, trim, insert, MIN_CLIP, indexOf, splitAt, clipAt } from './ops.js';
import { getTranscript } from './api.js';
import { transcriptStamp } from './player.js';
import { player } from './player.js';
import { $, el, clamp, fmtT, rafBatch, bsearch, fmtDur, toast } from './util.js';

const PAD = 16;              // px before story time 0
const TRACK_H = 84;
const SNAP_PX = 6;
const ZMIN = 4, ZMAX = 400;  // px per second

let zoom = +localStorage.getItem('studio.zoom') || 60;
const nodes = new Map();     // clip id -> {node, film, label, meta, filmKey, filmIn}
let scroll, content, track, ruler, phEl, insertEl, snapEl;
let drag = null;             // active pointer gesture
let fitPending = true;
let rulerKey = '';

const xOf = t => PAD + t * zoom;
const tOfClientX = cx => (cx - content.getBoundingClientRect().left - PAD) / zoom;

export const getZoom = () => zoom;
export function setZoom(z, anchorClientX) {
  z = clamp(z, ZMIN, ZMAX);
  if (Math.abs(z - zoom) < 0.001) return;
  const r = scroll.getBoundingClientRect();
  const ax = anchorClientX ?? (r.left + Math.min(r.width / 2, Math.max(0, xOf(S.playhead) - scroll.scrollLeft)));
  const tAt = (ax - r.left + scroll.scrollLeft - PAD) / zoom;
  zoom = z;
  localStorage.setItem('studio.zoom', String(zoom));
  syncZoomSlider();
  renderNow();
  scroll.scrollLeft = PAD + tAt * zoom - (ax - r.left);
}
export function fit() {
  const T = total();
  if (!T) return;
  setZoom((scroll.clientWidth - PAD * 2 - 24) / T);
  scroll.scrollLeft = 0;
}
const toSlider = z => Math.round(1000 * Math.log(z / ZMIN) / Math.log(ZMAX / ZMIN));
const fromSlider = v => ZMIN * Math.pow(ZMAX / ZMIN, v / 1000);
function syncZoomSlider() { $('#zoom').value = toSlider(zoom); }

/* ---------- render ---------- */
const render = rafBatch(renderNow);

function renderNow() {
  const tl = S.tl;
  const { starts, total: T } = layout(tl);
  const vw = scroll.clientWidth;
  const width = Math.max(vw, PAD * 2 + T * zoom + 240);
  content.style.width = width + 'px';
  $('#tl-empty').hidden = tl.clips.length > 0;
  drawRuler(width);

  const seen = new Set();
  tl.clips.forEach((c, i) => {
    seen.add(c.id);
    let n = nodes.get(c.id);
    if (!n) n = makeNode(c);
    const m = mediaOf(c.media);
    const w = Math.max(2, dur(c) * zoom);
    if (!(drag?.kind === 'move' && drag.id === c.id && drag.active)) {
      n.node.style.transform = `translateX(${xOf(starts[i])}px)`;
    }
    n.node.style.width = w + 'px';
    n.node.classList.toggle('selected', c.id === S.sel);
    n.node.classList.toggle('narrow', w < 46);
    n.node.classList.toggle('missing', !m);
    const text = c.label || m?.name || 'Missing footage';
    if (n.labelText !== text) { n.label.textContent = text; n.labelText = text; }
    const mt = fmtDur(dur(c));
    if (n.metaText !== mt) { n.meta.textContent = mt; n.metaText = mt; }
    n.node.dataset.tip = `${text} · ${fmtDur(dur(c))} · from ${fmtT(c.in)} to ${fmtT(c.out)} of “${m?.name || '?'}”.` +
      (c.cover?.length ? ' The orange line marks b-roll over its sound.' : '') + ' Drag to move, drag edges to trim.';
    n.node.dataset.index = i;
    drawFilm(n, c, m, w);
    drawCover(n, c);
    if (n.node.parentNode !== track) track.append(n.node);
  });
  for (const [id, n] of nodes) if (!seen.has(id)) { n.node.remove(); nodes.delete(id); }
  $('#story-meta').textContent = tl.clips.length
    ? `${tl.clips.length} clip${tl.clips.length > 1 ? 's' : ''} · ${fmtDur(T)}`
    : '';
  drawPlayhead();
}

function makeNode(c) {
  const film = el('div.film');
  const label = el('span.clip-label');
  const meta = el('span.clip-meta');
  const node = el('div.clip', { dataset: { id: c.id } },
    film, el('div.clip-text', {}, label, meta),
    el('div.handle.l', { 'data-tip': 'Drag to trim the start' }),
    el('div.handle.r', { 'data-tip': 'Drag to trim the end' }));
  const cover = el('div.cover-lane');
  node.append(cover);
  const n = { node, film, label, meta, cover, filmKey: '', filmIn: 0, coverKey: '' };
  nodes.set(c.id, n);
  return n;
}

/* Cover: a thin lane along the top of the clip marking where other footage shows over its
   sound (`studio timeline cover`). Pieces sit on the clip's own media clock (piece.at). */
function drawCover(n, c) {
  const key = `${c.in}|${c.out}|${zoom.toFixed(3)}|${JSON.stringify(c.cover || null)}`;
  if (n.coverKey === key) return;
  n.coverKey = key;
  const marks = (c.cover || []).map(p => {
    const a = Math.max(c.in, p.at), b = Math.min(c.out, p.at + p.out - p.in);
    if (b - a < 0.05) return null;
    return el('span', { style: { left: (a - c.in) * zoom + 'px', width: (b - a) * zoom + 'px' } });
  }).filter(Boolean);
  n.cover.replaceChildren(...marks);
  n.node.classList.toggle('covered', marks.length > 0);
}

/* Filmstrip: tiles sampled from the media's keyframes at the source time each tile covers. */
const imgCache = new Map();
function kfUrl(mid, kf) { return `/files/media/${mid}/${kf.file}`; }
function drawFilm(n, c, m, w) {
  const kfs = m?.keyframes || [];
  const ar = m?.width && m?.height ? m.width / m.height : 16 / 9;
  const tileW = clamp(Math.round(TRACK_H * ar), 30, 120);
  const inGesture = drag && drag.id === c.id && drag.kind === 'trim';
  if (inGesture) {   // keep frames aligned to source time without rebuilding
    n.film.style.transform = `translateX(${(n.filmIn - c.in) * zoom}px)`;
    return;
  }
  const key = `${c.in}|${c.out}|${zoom.toFixed(3)}|${kfs.length}|${tileW}`;
  if (n.filmKey === key) return;
  n.filmKey = key; n.filmIn = c.in;
  n.film.style.transform = '';
  if (!kfs.length) { n.film.replaceChildren(); n.film.classList.add('nofilm'); return; }
  n.film.classList.remove('nofilm');
  const count = Math.min(300, Math.ceil(w / tileW) + 1);
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const st = c.in + (i * tileW + tileW / 2) / zoom;
    const k = Math.max(0, bsearch(kfs, st, f => f.t));
    const url = kfUrl(m.id, kfs[k]);
    let img = imgCache.get(url)?.cloneNode();
    if (!img) {
      img = new Image();
      img.decoding = 'async'; img.loading = 'lazy'; img.draggable = false;
      img.src = url;
      imgCache.set(url, img);
      img = img.cloneNode();
    }
    img.style.width = tileW + 'px';
    img.style.left = i * tileW + 'px';
    frag.append(img);
  }
  n.film.replaceChildren(frag);
}

function drawRuler(width) {
  const key = `${zoom.toFixed(3)}|${width}`;
  if (key === rulerKey) return;
  rulerKey = key;
  const steps = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find(s => s * zoom >= 64) || 600;
  const minor = step / (step >= 1 && step % 5 === 0 ? 5 : 2);
  ruler.style.setProperty('--minor', minor * zoom + 'px');
  ruler.style.setProperty('--pad', PAD + 'px');
  const frag = document.createDocumentFragment();
  const maxT = (width - PAD) / zoom;
  for (let t = 0; t <= maxT; t += step) {
    const lab = step < 1 ? fmtT(t) : fmtT(t, 0);
    frag.append(el('span.tick', { text: lab, style: { transform: `translateX(${xOf(t)}px)` } }));
  }
  ruler.replaceChildren(frag);
}

/* ---------- playhead layer ---------- */
const drawPlayhead = () => {
  const x = xOf(S.playhead);
  phEl.style.transform = `translateX(${x}px)`;
  if (S.playing && !drag) {   // follow during playback
    const L = scroll.scrollLeft, W = scroll.clientWidth;
    if (x > L + W - 60 || x < L) scroll.scrollLeft = x - 80;
  }
};
const drawPlayheadBatched = rafBatch(drawPlayhead);

export function scrollToPlayhead() {
  const x = xOf(S.playhead), L = scroll.scrollLeft, W = scroll.clientWidth;
  if (x < L + 20 || x > L + W - 20) scroll.scrollLeft = x - W / 2;
}

/* ---------- pointer gestures ---------- */
function onPointerDown(e) {
  if (e.button !== 0) return;
  const handle = e.target.closest('.handle');
  const clipEl = e.target.closest('.clip');
  // moves capture once they start. WebKit throws for a pointer it doesn't consider active
  // (a synthetic or already-released one); the gesture must still start.
  if (!(clipEl && !handle)) try { content.setPointerCapture(e.pointerId); } catch { /* see above */ }
  if (handle && clipEl) return startTrim(e, clipEl.dataset.id, handle.classList.contains('l') ? 'L' : 'R');
  if (clipEl) return startMove(e, clipEl.dataset.id);
  startScrub(e);
}

function startScrub(e) {
  const wasPlaying = S.playing;
  if (wasPlaying) player.pause();
  drag = { kind: 'scrub', wasPlaying };
  player.scrubbing = true;
  document.body.classList.add('scrubbing');
  if (!e.target.closest('.tl-ruler') && !e.target.closest('.tl-playhead')) select(null);
  setPlayhead(tOfClientX(e.clientX), { from: 'scrub' });
}

function startMove(e, id) {
  drag = { kind: 'move', id, x0: e.clientX, y0: e.clientY, active: false, sl0: scroll.scrollLeft };
}

function activateMove() {
  const tl = S.tl, i = indexOf(tl, drag.id);
  const n = nodes.get(drag.id);
  drag.active = true;
  drag.from = i;
  drag.origX = xOf(layout(tl).starts[i]);
  drag.rest = tl.clips.filter(c => c.id !== drag.id);
  drag.d = dur(tl.clips[i]);
  n.node.classList.add('dragging');
  content.classList.add('reordering');
  document.body.classList.add('grabbing');
}

function moveFrame(e) {
  const n = nodes.get(drag.id);
  const dx = e.clientX - drag.x0 + (scroll.scrollLeft - drag.sl0);
  n.node.style.transform = `translate(${drag.origX + dx}px, -4px)`;
  // insertion index from the dragged block's centre against the other clips' centres
  const centre = (drag.origX + dx - PAD) / zoom + drag.d / 2;
  let t = 0, idx = 0;
  for (const c of drag.rest) { if (t + dur(c) / 2 < centre) idx++; t += dur(c); }
  drag.to = idx;
  // slide the others aside to show where it lands
  let x = 0;
  drag.rest.forEach((c, k) => {
    if (k === idx) x += drag.d;
    const m = nodes.get(c.id);
    if (m) m.node.style.transform = `translateX(${xOf(x)}px)`;
    x += dur(c);
  });
  let slot = 0;
  for (let k = 0; k < idx; k++) slot += dur(drag.rest[k]);
  showInsert(xOf(slot));
}

function startTrim(e, id, side) {
  const c = S.tl.clips.find(x => x.id === id);
  const m = mediaOf(c.media);
  gestureBegin();
  select(id);
  drag = { kind: 'trim', id, side, x0: e.clientX, sl0: scroll.scrollLeft, in0: c.in, out0: c.out,
    max: m?.duration ?? c.out, media: c.media, bounds: null, wasPlaying: S.playing };
  if (S.playing) player.pause();
  document.body.classList.add('trimming');
  getTranscript(c.media, transcriptStamp(m)).then(t => {
    if (!drag || drag.id !== id) return;
    const b = [];
    for (const w of t.words) { b.push(w.start, w.end); }
    drag.bounds = [...new Set(b)].sort((a, b) => a - b);
  });
}

function trimFrame(e) {
  const dt = (e.clientX - drag.x0 + (scroll.scrollLeft - drag.sl0)) / zoom;
  let t = drag.side === 'L' ? drag.in0 + dt : drag.out0 + dt;
  let snapped = false;
  if (!e.altKey && drag.bounds?.length) {
    const k = bsearch(drag.bounds, t, x => x);
    const cands = [drag.bounds[k], drag.bounds[k + 1]].filter(x => x != null);
    let best = null;
    for (const cand of cands) if (best == null || Math.abs(cand - t) < Math.abs(best - t)) best = cand;
    if (best != null && Math.abs(best - t) * zoom <= SNAP_PX) { t = best; snapped = true; }
  }
  let inn = drag.in0, out = drag.out0;
  if (drag.side === 'L') inn = clamp(t, 0, out - MIN_CLIP);
  else out = clamp(t, inn + MIN_CLIP, drag.max);
  gesturePreview(trim(S.gesture, drag.id, inn, out));
  player.peek(drag.media, drag.side === 'L' ? inn : Math.max(inn, out - 0.04));
  // indicator at the moving edge
  const i = indexOf(S.tl, drag.id), st = layout(S.tl).starts[i];
  const edgeX = drag.side === 'L' ? xOf(st) : xOf(st + out - inn);
  snapEl.hidden = !snapped;
  if (snapped) snapEl.style.transform = `translateX(${edgeX}px)`;
  drag.edge = drag.side === 'L' ? inn : out;
  emit('trimming', { id: drag.id, side: drag.side, t: drag.edge, inn, out });
}

let lastMoveEvent = null;
const frame = rafBatch(() => {
  const e = lastMoveEvent;
  if (!drag || !e) return;
  if (drag.kind === 'scrub') setPlayhead(tOfClientX(e.clientX), { from: 'scrub' });
  else if (drag.kind === 'move' && drag.active) moveFrame(e);
  else if (drag.kind === 'trim') trimFrame(e);
});

function onPointerMove(e) {
  if (!drag) return;
  if (drag.kind === 'move' && !drag.active) {
    if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 4) return;
    try { content.setPointerCapture(e.pointerId); } catch { /* released */ }
    activateMove();
  }
  lastMoveEvent = e;
  edgeScroll(e);
  frame();
}

function onPointerUp(e) {
  if (!drag) return;
  const d = drag;
  stopEdgeScroll();
  if (d.kind === 'scrub') {
    drag = null;
    player.scrubbing = false;
    document.body.classList.remove('scrubbing');
    setPlayhead(tOfClientX(e.clientX), { from: 'scrub-end' });
    if (d.wasPlaying) player.play();
  } else if (d.kind === 'move') {
    drag = null;
    document.body.classList.remove('grabbing');
    content.classList.remove('reordering');
    hideInsert();
    const n = nodes.get(d.id);
    n?.node.classList.remove('dragging');
    if (!d.active) {   // a click: select it and put the playhead where you clicked
      select(d.id);
      setPlayhead(tOfClientX(e.clientX), { from: 'click' });
    } else if (d.to != null && d.to !== d.from) {
      const clips = d.rest.slice();
      clips.splice(d.to, 0, S.tl.clips[d.from]);
      commit({ ...S.tl, clips }, 'Move clip', { select: d.id });
    } else render();
  } else if (d.kind === 'trim') {
    drag = null;
    snapEl.hidden = true;
    document.body.classList.remove('trimming');
    gestureEnd('Trim clip');
    emit('trimming', null);
    // leave the playhead at the edit so you can see the cut
    const i = indexOf(S.tl, d.id);
    if (i >= 0) {
      const st = layout(S.tl).starts[i], c = S.tl.clips[i];
      setPlayhead(d.side === 'L' ? st : st + dur(c) - 0.001, { from: 'trim' });
    }
    player.unpeek();
    render();
  }
}

function cancelGesture() {
  if (!drag) return false;
  const d = drag;
  drag = null;
  stopEdgeScroll();
  hideInsert(); snapEl.hidden = true;
  document.body.classList.remove('grabbing', 'trimming', 'scrubbing');
  content.classList.remove('reordering');
  nodes.get(d.id)?.node.classList.remove('dragging');
  if (d.kind === 'trim') { gestureCancel(); player.unpeek(); }
  player.scrubbing = false;
  renderNow();
  return true;
}
export { cancelGesture };

/* auto-scroll when dragging near the edges */
let esRaf = 0, esDir = 0;
function edgeScroll(e) {
  const r = scroll.getBoundingClientRect();
  esDir = e.clientX < r.left + 40 ? -1 : e.clientX > r.right - 40 ? 1 : 0;
  if (esDir && !esRaf) {
    const tick = () => {
      if (!esDir || !drag) { esRaf = 0; return; }
      scroll.scrollLeft += esDir * 12;
      frame();
      esRaf = requestAnimationFrame(tick);
    };
    esRaf = requestAnimationFrame(tick);
  }
}
function stopEdgeScroll() { esDir = 0; cancelAnimationFrame(esRaf); esRaf = 0; }

function showInsert(x) { insertEl.hidden = false; insertEl.style.transform = `translateX(${x - 1}px)`; }
function hideInsert() { insertEl.hidden = true; }

/* ---------- drops from the bin (moments, footage) ---------- */
export const DND_TYPE = 'application/x-studio-clip';
let dropIdx = null;
function dropIndexAt(clientX) {
  const t = tOfClientX(clientX);
  const { starts } = layout(S.tl);
  let idx = 0;
  S.tl.clips.forEach((c, i) => { if (starts[i] + dur(c) / 2 < t) idx = i + 1; });
  return [idx, idx < starts.length ? starts[idx] : layout(S.tl).total];
}
function onDragOver(e) {
  if (!e.dataTransfer.types.includes(DND_TYPE)) return;
  e.preventDefault();
  e.stopPropagation();
  e.dataTransfer.dropEffect = 'copy';
  const [idx, t] = dropIndexAt(e.clientX);
  dropIdx = idx;
  showInsert(xOf(t));
  $('#story').classList.add('drop-target');
}
function onDragLeave(e) {
  if (e.relatedTarget && scroll.contains(e.relatedTarget)) return;
  hideInsert(); $('#story').classList.remove('drop-target');
}
function onDrop(e) {
  if (!e.dataTransfer.types.includes(DND_TYPE)) return;
  e.preventDefault(); e.stopPropagation();
  hideInsert(); $('#story').classList.remove('drop-target');
  let p;
  try { p = JSON.parse(e.dataTransfer.getData(DND_TYPE)); } catch { return; }
  const [idx] = dropIndexAt(e.clientX);
  addClip(p, idx);
}

/** Add {media, in, out, label?, moment?} to the story at idx (default: end). `moment` is the
 *  moment n it came from: Copy post and "In story" follow it through trims. */
export function addClip(p, idx = null) {
  const m = mediaOf(p.media);
  if (!m) return;
  if (!m.has_video) { toast('That file has no picture, so it can’t go in the story yet.', { kind: 'warn' }); return; }
  const a = clamp(+p.in || 0, 0, m.duration), b = clamp(p.out != null ? +p.out : m.duration, 0, m.duration);
  if (b - a < MIN_CLIP) return;
  const [tl, c] = insert(S.tl, idx, { media: p.media, in: a, out: b, ...(p.label ? { label: p.label } : {}),
    ...(p.moment != null ? { moment: +p.moment } : {}) });
  commit(tl, 'Add clip', { select: c.id });
  const i = indexOf(S.tl, c.id);
  setPlayhead(layout(S.tl).starts[i] + 0.0001, { from: 'add' });
  if (fitPending || layout(S.tl).total * zoom > scroll.clientWidth - 40) fit();
  scrollToPlayhead();
  flashClips([c.id]);
}

/** Someone else removed clips: leave a fading red ghost where each one was, so a removal
 *  is as visible as an addition. */
function ghostRemoved(prevTl) {
  const keep = new Set(S.tl.clips.map(c => c.id));
  const { starts } = layout(prevTl);
  prevTl.clips.forEach((c, i) => {
    if (keep.has(c.id)) return;
    const g = el('div.clip-ghost', { style: { transform: `translateX(${xOf(starts[i])}px)`, width: Math.max(4, dur(c) * zoom) + 'px' } },
      el('span', { text: `✕ ${c.label || 'removed'}` }));
    track.append(g);
    setTimeout(() => g.remove(), 2600);
  });
}

export function flashClips(ids) {
  requestAnimationFrame(() => {
    for (const id of ids) {
      const n = nodes.get(id)?.node;
      if (!n) continue;
      n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash');
    }
  });
}

/* ---------- commands used by keyboard & buttons ---------- */
export function splitAtPlayhead() {
  const at = clipAt(S.tl, S.playhead);
  if (!at) return toast('Nothing to split — the story is empty.');
  const [tl, rid] = splitAt(S.tl, at.clip.id, at.src);
  if (!rid) return toast('Move the playhead a little further inside the clip to split it.');
  commit(tl, 'Split clip', { select: rid });
  flashClips([at.clip.id, rid]);
}

/* ---------- init ---------- */
export function initTimeline() {
  scroll = $('#tl-scroll'); content = $('#tl-content'); track = $('#tl-track');
  ruler = $('#tl-ruler'); phEl = $('#tl-playhead'); insertEl = $('#tl-insert'); snapEl = $('#tl-snap');
  $('#tl-empty').replaceChildren(
    el('div.empty-title', { text: 'Your story is empty' }),
    el('div.empty-sub', { text: 'Pick a moment on the left and press “Add to story”, or drag a moment or a clip of footage down here.' }));
  content.addEventListener('pointerdown', onPointerDown);
  content.addEventListener('pointermove', onPointerMove);
  content.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointerup', () => { if (drag?.kind === 'move' && !drag.active) drag = null; });
  content.addEventListener('pointercancel', () => cancelGesture());
  content.addEventListener('lostpointercapture', e => { if (drag && e.pointerType !== 'mouse') cancelGesture(); });
  content.addEventListener('dblclick', e => {
    const c = e.target.closest('.clip');
    if (c) { select(c.dataset.id); emit('openTab', 'clip'); }
  });
  scroll.addEventListener('wheel', e => {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      setZoom(zoom * Math.exp(-e.deltaY * 0.01), e.clientX);
    } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault();
      scroll.scrollLeft += e.deltaY;
    }
  }, { passive: false });
  // WebKit (Safari, the Mac app): a trackpad pinch arrives as gesture events, not as the
  // ctrl+wheel Chrome sends
  let pinch0 = null;
  scroll.addEventListener('gesturestart', e => { e.preventDefault(); pinch0 = zoom; });
  scroll.addEventListener('gesturechange', e => { e.preventDefault(); if (pinch0) setZoom(pinch0 * e.scale, e.clientX); });
  scroll.addEventListener('gestureend', e => { e.preventDefault(); pinch0 = null; });
  scroll.addEventListener('dragover', onDragOver);
  scroll.addEventListener('dragleave', onDragLeave);
  scroll.addEventListener('drop', onDrop);
  $('#zoom').addEventListener('input', e => setZoom(fromSlider(+e.target.value)));
  $('#btn-zoom-in').onclick = () => setZoom(zoom * 1.4);
  $('#btn-zoom-out').onclick = () => setZoom(zoom / 1.4);
  $('#btn-fit').onclick = fit;
  $('#btn-split').onclick = splitAtPlayhead;
  syncZoomSlider();
  new ResizeObserver(() => { rulerKey = ''; render(); }).observe(scroll);

  let prev = new Map();
  on('timeline', d => {
    if (d?.remote) {
      const changed = S.tl.clips.filter(c => { const p = prev.get(c.id); return !p || p.in !== c.in || p.out !== c.out; }).map(c => c.id);
      flashClips(changed);
      if (d.prev) ghostRemoved(d.prev);
      const st = $('#story');
      st.classList.remove('remote-flash'); void st.offsetWidth; st.classList.add('remote-flash');
    }
    if (!d?.preview) prev = new Map(S.tl.clips.map(c => [c.id, c]));
    render();
  });
  on('project', () => { fitPending = true; });
  on('server', () => {
    render();
    if (fitPending && S.tl.clips.length) { fitPending = false; requestAnimationFrame(fit); }
  });
  on('select', render);
  on('playhead', drawPlayheadBatched);
  render();
}
