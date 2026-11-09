// Right pane: Clip (trim, split, edit by words) | Posts | Activity | Tell the agent.

import { S, on, emit, commit, select, setPlayhead, gestureBegin, gesturePreview, gestureEnd,
  mediaOf, selClip, selIndex, setWordSel, focusPayload, project, sendFocus } from './store.js';
import { trim, splitAt, duplicate, remove, cutRange, insert, setLabel, setFit, layout, indexOf, clipAt, dur, MIN_CLIP, diffStory } from './ops.js';
import { clipFit, FIT_WORDS } from './formats.js';
import * as api from './api.js';
import { player, transcriptStamp } from './player.js';
import { runAI, needsMoments, runAIAll } from './bin.js';
import { activity } from './live.js';
import { splitAtPlayhead, flashClips, scrollToPlayhead } from './timeline.js';
import { $, $$, el, fmtT, fmtDur, clamp, toast, copyText, timeAgo, bsearch, rafBatch, debounce, isAgent } from './util.js';

let tab = 'clip';
export function openTab(name) {
  tab = name;
  for (const b of $$('#inspector .tab')) b.classList.toggle('active', b.dataset.tab === name);
  for (const s of $$('#inspector .tab-body')) s.classList.toggle('active', s.id === 'tab-' + name);
  if (name === 'activity') $('#activity-dot').hidden = true;
  renderTab();
}
function renderTab() {
  if (tab === 'clip') renderClip();
  else if (tab === 'posts') renderPosts();
  else if (tab === 'activity') renderActivity();
  else if (tab === 'agent') renderAgent();
}

/* =====================================================================
   CLIP TAB
   ===================================================================== */
let C = null;  // current clip-tab view: {clipId, media, words, spans, trimBox, ...}

function subject() {
  const c = selClip();
  if (c) return { clip: c, m: mediaOf(c.media) };
  const m = mediaOf(S.media);
  return m ? { clip: null, m } : null;
}

function renderClip() {
  const box = $('#tab-clip');
  const sub = subject();
  if (!sub || !sub.m) {
    C = null;
    const todo = needsMoments();
    box.replaceChildren(el('div.empty-pane', {},
      todo.length ? el('div.cta-card', {},
        el('div.empty-title', { text: 'Next: let the agent pick the best moments' }),
        el('div.empty-sub', { text: `The agent watches ${todo.length === 1 ? 'your footage' : `all ${todo.length} files`}, picks the parts worth posting and drafts the posts. About a minute; keep working meanwhile.` }),
        el('button.btn.accent', { text: todo.length > 1 ? `✦ Find moments in all ${todo.length}` : '✦ Find moments', onclick: runAIAll })) : null,
      el('div.empty-title', { text: 'Nothing selected' }),
      el('div.empty-sub', { text: 'Click a clip in the Story below to trim it, split it, or edit it by its words. Or click footage on the left to read what was said.' }),
      el('div.kbd-help', {},
        kbd('Space', 'play / pause'), kbd('S', 'split at playhead'), kbd('⌫', 'remove clip'),
        kbd('← →', 'one frame'), kbd('⇧ ← →', 'one second'), kbd('J K L', 'back · pause · play'),
        kbd('⌘Z', 'undo'), kbd('⌘ scroll', 'zoom story'))));
    return;
  }
  const { clip, m } = sub;
  const parts = [];
  if (clip) {
    const i = selIndex();
    const labelIn = el('input.label-input', { value: clip.label || '', placeholder: m.name, 'data-tip': 'Name this clip (the agent sees this name too)' });
    labelIn.addEventListener('change', () => commit(setLabel(S.tl, clip.id, labelIn.value.trim()), 'Rename clip'));
    labelIn.addEventListener('keydown', e => { if (e.key === 'Enter') labelIn.blur(); e.stopPropagation(); });
    parts.push(el('div.insp-head', {},
      el('div.insp-kicker', { text: `Clip ${i + 1} of ${S.tl.clips.length} · ${m.name}` }),
      labelIn,
      el('div.insp-len', {}, el('span.len-big', { id: 'insp-len' }), el('span.len-sub', { id: 'insp-range' }))));
    parts.push(trimBox(clip, m));
    parts.push(fitBox(clip, m));
    parts.push(el('div.insp-actions', {},
      el('button.btn.sm', { id: 'insp-split', text: '✂ Split here', 'data-tip': 'Cut this clip in two at the playhead (S)', onclick: splitAtPlayhead }),
      el('button.btn.sm', { text: '⧉ Duplicate', 'data-tip': 'Make a copy right after it', onclick: () => { const [tl, id] = duplicate(S.tl, clip.id); commit(tl, 'Duplicate clip', { select: id }); flashClips([id]); } }),
      el('button.btn.sm', { text: '▶ Play clip', 'data-tip': 'Play just this clip from its start', onclick: () => { select(clip.id, { seek: true }); player.play(); } }),
      el('button.btn.sm.danger', { text: 'Remove', 'data-tip': 'Take it out of the story (Delete). Undo brings it back.', onclick: () => removeSelected() })));
  } else {
    parts.push(el('div.insp-head', {},
      el('div.insp-kicker', { text: 'Footage' }),
      el('div.insp-title', { text: m.name }),
      el('div.insp-len', {}, el('span.len-big', { text: fmtDur(m.duration) }),
        el('span.len-sub', { text: `${m.width && m.height ? `${m.width}×${m.height} · ` : ''}${m.has_audio ? 'with sound' : 'no sound'}` }))),
      el('div.insp-actions', {},
        m.has_video ? el('button.btn.sm', { text: '▶ Preview', onclick: () => player.previewSource(m.id) }) : null,
        m.has_video ? el('button.btn.sm.primary', { text: '+ Add whole clip to story', onclick: () => import('./timeline.js').then(t => t.addClip({ media: m.id, in: 0, out: m.duration })) }) : null));
  }
  const tbox = el('div.transcript', { id: 'transcript' }, el('div.empty-small', {}, el('span.spinner'), ' Loading words…'));
  parts.push(el('div.sec-head', {}, el('h3', { text: 'Words' }),
    el('span.sec-sub', { text: clip ? 'click to jump · drag across words to keep or cut' : 'drag across words to make a clip' })));
  parts.push(el('div.wsel-bar.idle', { id: 'wsel-bar' }));
  parts.push(tbox);
  box.replaceChildren(...parts);
  C = { clipId: clip?.id || null, mid: m.id, stamp: transcriptStamp(m), words: null, spans: [], inA: -1, inB: -1, now: -1, sel: null };
  updateClip();
  const view = C;
  api.getTranscript(m.id, transcriptStamp(m)).then(t => { if (C === view) buildTranscript(t, m); });
}

function kbd(k, what) { return el('div.kb', {}, el('kbd', { text: k }), el('span', { text: what })); }

/* --- frame: crop to fill it, or show the whole picture over a blurred copy --- */
const FIT_TIPS = {
  fill: 'Zoom in until the picture fills the frame. Edges that don’t fit are cut off. Best for camera footage.',
  fit: 'Show the whole picture, with a blurred copy of it filling the empty space. Best for screen recordings and wide shots.',
};
function fitBox(clip, m) {
  const btn = mode => el('button', { text: FIT_WORDS[mode], dataset: { fit: mode }, 'data-tip': FIT_TIPS[mode],
    onclick: () => { const c = selClip(); if (c) commit(setFit(S.tl, c.id, mode), FIT_WORDS[mode]); } });
  const box = el('div.fitbox', {},
    el('div.sec-head', {}, el('h3', { text: 'Frame' }), el('span.sec-sub', { id: 'fit-why' })),
    el('div.seg-ctl.fit-ctl', {}, btn('fill'), btn('fit')));
  requestAnimationFrame(() => updateFit(clip, m));
  return box;
}
function updateFit(c, m) {
  const cur = clipFit(c, m, S.tl.aspect), chosen = c.fit === 'fill' || c.fit === 'fit';
  for (const b of $$('#tab-clip .fit-ctl button')) b.classList.toggle('on', b.dataset.fit === cur);
  const why = $('#fit-why');
  if (why) why.textContent = chosen ? 'your choice' : cur === 'fit' ? 'picked for this footage: a screen recording' : 'picked for this footage';
}

/* --- trim box: whole source as a filmstrip, the clip as a window with two handles --- */
function trimBox(clip, m) {
  const kfs = m.keyframes || [];
  const film = el('div.tb-film');
  const N = 12;
  for (let i = 0; i < N; i++) {
    const t = (i + 0.5) / N * m.duration;
    const k = Math.max(0, bsearch(kfs, t, f => f.t));
    film.append(kfs.length ? el('img', { src: `/files/media/${m.id}/${kfs[k].file}`, loading: 'lazy', decoding: 'async', draggable: 'false' }) : el('div.tb-ph'));
  }
  const win = el('div.tb-win', {}, el('div.tb-h.l', { 'data-tip': 'Drag to change where it starts' }), el('div.tb-h.r', { 'data-tip': 'Drag to change where it ends' }));
  const ph = el('div.tb-ph-line');
  const bar = el('div.tb-bar', {}, film, el('div.tb-shade.l'), el('div.tb-shade.r'), win, ph);
  const startIn = numIn('start'), endIn = numIn('end');
  const box = el('div.trimbox', {},
    el('div.sec-head', {}, el('h3', { text: 'Trim' }), el('span.sec-sub', { text: 'drag the edges · or type exact times' })),
    bar,
    el('div.tb-fields', {},
      field('Starts at', startIn, -0.1, 0.1, 'start'),
      field('Ends at', endIn, -0.1, 0.1, 'end')));
  bar.addEventListener('pointerdown', e => trimDown(e, bar));
  bar.addEventListener('pointermove', trimMove);
  bar.addEventListener('pointerup', trimUp);
  bar.addEventListener('pointercancel', trimUp);
  return box;
}
function numIn(which) {
  const inp = el('input.num', { type: 'number', step: '0.1', min: '0', id: 'tb-' + which });
  inp.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') inp.blur(); });
  inp.addEventListener('change', () => setEdge(which, +inp.value));
  return inp;
}
function field(label, inp, minus, plus, which) {
  return el('label.tb-field', {}, el('span', { text: label }),
    el('button.btn.xs.ghost', { text: '−', 'data-tip': '0.1 second earlier', onclick: e => { e.preventDefault(); nudgeEdge(which, minus); } }),
    inp, el('span.unit', { text: 's' }),
    el('button.btn.xs.ghost', { text: '+', 'data-tip': '0.1 second later', onclick: e => { e.preventDefault(); nudgeEdge(which, plus); } }));
}
function nudgeEdge(which, d) {
  const c = selClip(); if (!c) return;
  setEdge(which, (which === 'start' ? c.in : c.out) + d);
}
function setEdge(which, v) {
  const c = selClip(); if (!c || !isFinite(v)) return;
  const m = mediaOf(c.media);
  let a = c.in, b = c.out;
  if (which === 'start') a = clamp(v, 0, b - MIN_CLIP); else b = clamp(v, a + MIN_CLIP, m?.duration ?? b);
  commit(trim(S.tl, c.id, a, b), 'Trim clip');
}

let tdrag = null;
function trimDown(e, bar) {
  const c = selClip(); if (!c) return;
  const m = mediaOf(c.media);
  const h = e.target.closest('.tb-h');
  const r = bar.getBoundingClientRect();
  const t = clamp((e.clientX - r.left) / r.width * m.duration, 0, m.duration);
  try { bar.setPointerCapture(e.pointerId); } catch { /* WebKit: pointer not active; the gesture still works */ }
  if (!h) {   // click on the strip: preview that frame of the source
    tdrag = { kind: 'peek', r, m };
    player.peek(m.id, t);
    return;
  }
  gestureBegin();
  tdrag = { kind: 'edge', side: h.classList.contains('l') ? 'L' : 'R', r, m, id: c.id, bounds: wordBounds() };
}
function wordBounds() {
  if (!C?.words) return null;
  const b = []; for (const w of C.words) b.push(w.start, w.end);
  return [...new Set(b)].sort((x, y) => x - y);
}
const trimMove = e => {
  if (!tdrag) return;
  const { r, m } = tdrag;
  let t = clamp((e.clientX - r.left) / r.width * m.duration, 0, m.duration);
  if (tdrag.kind === 'peek') { player.peek(m.id, t); return; }
  if (!e.altKey && tdrag.bounds?.length) {
    const k = bsearch(tdrag.bounds, t, x => x);
    for (const cand of [tdrag.bounds[k], tdrag.bounds[k + 1]]) {
      if (cand != null && Math.abs(cand - t) / m.duration * r.width <= 6) { t = cand; break; }
    }
  }
  const c = S.gesture.clips.find(x => x.id === tdrag.id);
  let a = c.in, b = c.out;
  if (tdrag.side === 'L') a = clamp(t, 0, b - MIN_CLIP); else b = clamp(t, a + MIN_CLIP, m.duration);
  gesturePreview(trim(S.gesture, tdrag.id, a, b));
  player.peek(m.id, tdrag.side === 'L' ? a : b - 0.04);
};
function trimUp() {
  if (!tdrag) return;
  const d = tdrag; tdrag = null;
  if (d.kind === 'edge') {
    gestureEnd('Trim clip');
    const i = indexOf(S.tl, d.id);
    if (i >= 0) { const st = layout(S.tl).starts[i]; setPlayhead(d.side === 'L' ? st : st + dur(S.tl.clips[i]) - 0.001, { from: 'trim' }); }
  }
  player.unpeek();
}

/** Cheap refresh of the dynamic bits (called on every timeline change, incl. drag previews). */
const updateClip = rafBatch(() => {
  if (!C) return;
  const c = selClip();
  if (C.clipId && !c) { renderClip(); return; }
  if (!c) { updateNow(); return; }
  const m = mediaOf(c.media);
  const len = $('#insp-len'), rng = $('#insp-range');
  if (len) len.textContent = fmtDur(dur(c));
  if (rng) rng.textContent = `from ${fmtT(c.in)} to ${fmtT(c.out)} of the original`;
  updateFit(c, m);
  const D = m?.duration || c.out || 1;
  const win = $('#tab-clip .tb-win');
  if (win) {
    const l = c.in / D * 100, w = (c.out - c.in) / D * 100;
    win.style.left = l + '%'; win.style.width = w + '%';
    $('#tab-clip .tb-shade.l').style.width = l + '%';
    $('#tab-clip .tb-shade.r').style.left = (l + w) + '%';
  }
  const si = $('#tb-start'), ei = $('#tb-end');
  if (si && document.activeElement !== si) si.value = c.in.toFixed(2);
  if (ei && document.activeElement !== ei) ei.value = c.out.toFixed(2);
  markInOut(c);
  updateNow();
});

function markInOut(c) {
  if (!C?.words) return;
  const ws = C.words;
  // first word ending after `in`, last word starting before `out`
  let a = 0; while (a < ws.length && ws[a].end <= c.in + 0.01) a++;
  let b = bsearch(ws, c.out - 0.01, w => w.start) + 1;
  if (b < a) b = a;
  const [pa, pb] = [C.inA, C.inB];
  if (pa === a && pb === b) return;
  const lo = Math.min(a, pa < 0 ? a : pa), hi = Math.max(b, pb < 0 ? b : pb);
  const touch = pa < 0 ? [0, ws.length] : [lo, hi];
  for (let i = touch[0]; i < touch[1]; i++) C.spans[i]?.classList.toggle('in', i >= a && i < b);
  C.inA = a; C.inB = b;
}

/** Highlight the word being spoken under the playhead. */
function updateNow() {
  if (!C?.words) return;
  let idx = -1;
  const at = clipAt(S.tl, S.playhead);
  const split = $('#insp-split');
  const c = selClip();
  if (split && c) {
    const ok = at && at.clip.id === c.id && at.local > MIN_CLIP && dur(c) - at.local > MIN_CLIP;
    split.disabled = !ok;
    split.dataset.tip = ok ? 'Cut this clip in two at the playhead (S)' : 'Move the playhead inside this clip to split it';
  }
  if (at && at.clip.media === C.mid) {
    const k = bsearch(C.words, at.src, w => w.start);
    if (k >= 0 && at.src < C.words[k].end + 0.15) idx = k;
  }
  if (idx === C.now) return;
  C.spans[C.now]?.classList.remove('now');
  C.spans[idx]?.classList.add('now');
  C.now = idx;
  if (idx >= 0 && S.playing) {
    const sp = C.spans[idx], box = $('#transcript');
    const r = sp.offsetTop - box.offsetTop;
    if (r < box.scrollTop + 20 || r > box.scrollTop + box.clientHeight - 40) box.scrollTop = r - box.clientHeight / 3;
  }
}

function buildTranscript(t, m) {
  const box = $('#transcript');
  if (!box) return;
  C.words = t.words;
  if (!t.words.length) {
    const why = !m.has_audio ? 'This footage has no sound, so there are no words — trim it with the handles above.'
      : m.steps?.transcribe ? 'No speech was found in this footage.'
      : 'Writing out the words… this runs on your Mac and takes a moment.';
    box.replaceChildren(el('div.empty-small', { text: why }));
    return;
  }
  // group words into segments (paragraph per segment, with a time marker)
  const segs = t.segments.length ? t.segments : [{ start: 0, end: 1e9 }];
  const frag = document.createDocumentFragment();
  const spans = new Array(t.words.length);
  let wi = 0;
  for (let s = 0; s < segs.length && wi < t.words.length; s++) {
    const segEnd = s + 1 < segs.length ? segs[s + 1].start : 1e9;
    const p = el('p.seg', {}, el('span.seg-t', { text: fmtT(segs[s].start, 0), dataset: { t: segs[s].start } }));
    while (wi < t.words.length && t.words[wi].start < segEnd - 0.001) {
      const sp = document.createElement('span');
      sp.className = 'w';
      sp.textContent = t.words[wi].w;
      sp.dataset.i = wi;
      p.append(sp, ' ');
      spans[wi] = sp;
      wi++;
    }
    frag.append(p);
  }
  while (wi < t.words.length) {   // leftovers past the last segment
    const sp = el('span.w', { text: t.words[wi].w, dataset: { i: wi } });
    frag.append(sp, ' '); spans[wi] = sp; wi++;
  }
  box.replaceChildren(frag);
  C.spans = spans; C.inA = -1; C.inB = -1; C.now = -1;
  renderSelBar();
  box.addEventListener('pointerdown', wordDown);
  box.addEventListener('pointerover', wordOver);
  box.addEventListener('dblclick', wordEdit);
  updateClip();
  // bring the clip's words into view
  const c = selClip();
  if (c) {
    const k = Math.max(0, bsearch(t.words, c.in, w => w.start));
    const sp = spans[k];
    if (sp) box.scrollTop = sp.offsetTop - box.offsetTop - 24;
  }
}

/* --- fix a misheard word: double-click it --- */
const wordCore = w => String(w).replace(/^[“"'‘(\[¿¡]+/, '').replace(/[”"'’)\].,!?;:…]+$/, '').replace(/['’]s$/i, '');
function closeWordEdit() {
  const ed = $('#transcript .w-edit');
  if (!ed) return;
  ed.previousElementSibling?.removeAttribute('hidden');
  ed.remove();
}
function wordEdit(e) {
  const sp = e.target.closest('.w');
  if (!sp || !C?.words) return;
  e.preventDefault();
  closeWordEdit();
  window.getSelection()?.removeAllRanges();
  const i = +sp.dataset.i, mid = C.mid, core = wordCore(C.words[i].w) || C.words[i].w;
  const inp = el('input.w-edit-in', { value: core, size: Math.max(5, core.length + 3), 'aria-label': 'What was actually said', spellcheck: 'false' });
  const all = el('input', { type: 'checkbox' });
  const go = async () => {
    const right = inp.value.trim();
    if (!right || (right === core && !all.checked)) return closeWordEdit();
    try {
      const r = await api.fixWord(mid, { index: i, wrong: core, right, all: all.checked });
      closeWordEdit();
      toast(!r.changed ? 'Nothing needed changing.'
        : all.checked ? `Fixed “${core}” → “${right}”: ${r.changed} word${r.changed > 1 ? 's' : ''} in ${r.files} file${r.files > 1 ? 's' : ''}. Captions use it now.`
        : `Fixed “${core}” → “${right}”. Captions use it now.`, { kind: 'good' });
    } catch (err) { toast('Couldn’t fix that word: ' + err.message, { kind: 'error' }); }
  };
  const ed = el('span.w-edit', {}, inp,
    el('label.w-edit-all', { 'data-tip': `Change every “${core}” in every file's words, not just this one` }, all, ` everywhere`),
    el('button.btn.xs.primary', { text: 'Fix', onclick: go }),
    el('button.btn.xs.ghost', { text: '✕', 'data-tip': 'Cancel', onclick: closeWordEdit }));
  for (const n of [inp, all]) n.addEventListener('keydown', ev => {
    ev.stopPropagation();
    if (ev.key === 'Enter') { ev.preventDefault(); go(); } else if (ev.key === 'Escape') closeWordEdit();
  });
  sp.hidden = true;
  sp.after(ed);
  inp.focus(); inp.select();
}

let wdrag = null;
function wordDown(e) {
  const sp = e.target.closest('.w');
  if (!sp || e.button !== 0) return;
  e.preventDefault();
  const i = +sp.dataset.i;
  if (e.shiftKey && C.sel) { setSel(Math.min(C.sel[0], i), Math.max(C.sel[1], i)); return; }
  wdrag = { a: i, moved: false };
}
function wordOver(e) {
  if (!wdrag) return;
  const sp = e.target.closest('.w');
  if (!sp) return;
  const i = +sp.dataset.i;
  if (i !== wdrag.a) wdrag.moved = true;
  setSel(Math.min(wdrag.a, i), Math.max(wdrag.a, i));
}
function wordUp(e) {
  if (!wdrag) return;
  const d = wdrag; wdrag = null;
  if (!d.moved) {
    // a click: clear selection and jump to the word
    setSel(null);
    seekWord(d.a);
  }
}
function seekWord(i) {
  const w = C.words[i];
  const c = selClip();
  if (c && w.start >= c.in - 0.05 && w.start < c.out) {
    const st = layout(S.tl).starts[indexOf(S.tl, c.id)];
    setPlayhead(st + clamp(w.start - c.in, 0, dur(c) - 0.001), { from: 'word' });
    scrollToPlayhead();
    return;
  }
  // a word outside the story: is it in another clip of the story?
  const k = S.tl.clips.findIndex(x => x.media === C.mid && w.start >= x.in && w.start < x.out);
  if (k >= 0) {
    setPlayhead(layout(S.tl).starts[k] + (w.start - S.tl.clips[k].in), { from: 'word' });
    scrollToPlayhead();
    return;
  }
  player.previewSource(C.mid, Math.max(0, w.start - 0.05), null, false);
}

function setSel(a, b) {
  const prev = C.sel;
  if (prev) for (let i = prev[0]; i <= prev[1]; i++) C.spans[i]?.classList.remove('sel');
  if (a == null) { C.sel = null; setWordSel(null); renderSelBar(); return; }
  C.sel = [a, b];
  for (let i = a; i <= b; i++) C.spans[i]?.classList.add('sel');
  const ws = C.words;
  setWordSel({ media: C.mid, start: ws[a].start, end: ws[b].end, text: ws.slice(a, b + 1).map(w => w.w).join(' ').slice(0, 400) });
  renderSelBar();
}

function selRange() {
  const [a, b] = C.sel, ws = C.words;
  return { a: ws[a].start, b: ws[b].end, next: ws[b + 1]?.start, prevEnd: ws[a - 1]?.end };
}

function renderSelBar() {
  const bar = $('#wsel-bar');
  if (!bar) return;
  if (!C?.sel) {   // keep the bar's space reserved so words never jump mid-drag
    bar.classList.add('idle');
    bar.replaceChildren(el('span.wsel-info', { text: C?.words?.length ? 'Tip: drag across words, then keep them or cut them out.' : '' }));
    return;
  }
  bar.classList.remove('idle');
  const r = selRange(), n = C.sel[1] - C.sel[0] + 1;
  const c = selClip();
  const overlaps = c && c.media === C.mid && Math.min(c.out, r.b) - Math.max(c.in, r.a) > 0.05;
  const m = mediaOf(C.mid);
  bar.hidden = false;
  bar.replaceChildren(...[
    el('span.wsel-info', { text: `${n} word${n > 1 ? 's' : ''} · ${fmtDur(r.b - r.a)}` }),
    c ? el('button.btn.xs.primary', { text: 'Keep only this', 'data-tip': 'Trim the clip to just these words', onclick: () => keepOnly(c, r, m) }) : null,
    c ? el('button.btn.xs', { text: 'Cut this out', disabled: !overlaps, 'data-tip': overlaps ? 'Remove these words from the clip (it splits around them)' : 'These words aren’t in the selected clip', onclick: () => cutOut(c, r) }) : null,
    el('button.btn.xs' + (c ? '' : '.primary'), { text: c ? '+ As new clip' : '+ Add to story', 'data-tip': 'Make a new clip from just these words', onclick: () => addRange(r, m, c) }),
    el('button.btn.xs.ghost', { text: '✕', 'data-tip': 'Clear selection', onclick: () => setSel(null) })].filter(Boolean));
}
const PAD_IN = 0.05, PAD_OUT = 0.12;
function keepOnly(c, r, m) {
  const a = clamp(Math.max(r.a - PAD_IN, r.prevEnd ?? 0), 0, m.duration);
  const b = clamp(Math.min(r.b + PAD_OUT, r.next ?? m.duration), 0, m.duration);
  commit(trim(S.tl, c.id, a, b), 'Keep only selected words');
  setSel(null);
  select(c.id, { seek: true });   // show what's left: the preview jumps to the trimmed clip
  flashClips([c.id]);
}
function cutOut(c, r) {
  const [tl, id] = cutRange(S.tl, c.id, r.a, r.next ?? r.b);
  if (tl === S.tl) return;
  const before = S.tl.clips.length;
  commit(tl, 'Cut words out', { select: id });
  setSel(null);
  const ids = S.tl.clips.filter(x => x.media === c.media).map(x => x.id);
  flashClips(ids);
  toast(S.tl.clips.length > before ? 'Cut out — the clip was split around those words.' : 'Cut out.', { actions: [{ label: 'Undo', run: () => emit('undo') }] });
}
function addRange(r, m, c) {
  const a = clamp(Math.max(r.a - PAD_IN, r.prevEnd ?? 0), 0, m.duration);
  const b = clamp(Math.min(r.b + PAD_OUT, r.next ?? m.duration), 0, m.duration);
  const at = c ? indexOf(S.tl, c.id) + 1 : null;
  import('./timeline.js').then(t => t.addClip({ media: m.id, in: a, out: b }, at));
  setSel(null);
}

export function removeSelected() {
  const c = selClip();
  if (!c) return toast('Click a clip in the Story first.');
  const i = selIndex();
  const tl = remove(S.tl, c.id);
  const next = tl.clips[Math.min(i, tl.clips.length - 1)];
  commit(tl, 'Remove clip', { select: next?.id || null });
  toast('Clip removed.', { actions: [{ label: 'Undo', run: () => emit('undo') }] });
}

/* =====================================================================
   POSTS TAB
   ===================================================================== */
const FIELDS = [
  ['title', 'Video title', 'YouTube / Shorts title'],
  ['short', 'Reels · TikTok · Shorts caption', 'Under the vertical video'],
  ['x', 'X post', 'Keep it under 280 characters', 280],
  ['linkedin', 'LinkedIn post', 'Longer, more context'],
];
const pkey = (mid, n, f) => `studio.post.${mid}.${n}.${f}`;   // pre-server edits (migrated)
const momentKey = mo => `${(+mo.start).toFixed(2)}-${(+mo.end).toFixed(2)}`;
const localEdits = new Map();   // edits typed but maybe not yet back from the server

/** The post for the selected clip (for "Copy post" from the story): the moment the clip was
 *  added from, if it still overlaps it; otherwise the moment covering most of the clip. */
export function matchingPost(m, c, posts) {
  if (!c || c.media !== m.id) return null;
  const mom = n => (m.moments || []).find(x => x.n === n);
  const over = mo => Math.min(c.out, mo.end) - Math.max(c.in, mo.start);
  if (c.moment != null && posts.some(p => p.n === c.moment)) {
    const mo = mom(c.moment);
    if (mo && over(mo) > 0) return c.moment;
  }
  let best = null, br = 0.2;    // at least a fifth of the clip
  for (const p of posts) {
    const mo = mom(p.n);
    if (!mo) continue;
    const r = over(mo) / Math.max(0.01, dur(c));
    if (r > br) { br = r; best = p.n; }
  }
  return best;
}

function renderPosts() {
  const box = $('#tab-posts');
  const c = selClip();
  const m = mediaOf(c?.media || S.media);
  if (!m) {
    box.replaceChildren(el('div.empty-pane', {}, el('div.empty-title', { text: 'Posts' }),
      el('div.empty-sub', { text: 'Pick footage on the left (or a clip in the Story) to see the post drafts the agent wrote for it.' })));
    return;
  }
  const dr = m.drafts || {};
  const posts = dr.posts || [];
  const head = el('div.insp-head', {}, el('div.insp-kicker', { text: 'Post drafts' }), el('div.insp-title', { text: m.name }),
    el('div.hint', { text: 'These are rough on purpose: rewrite them in your own voice. Your rewrites are saved, and the agent can read them.' }));
  if (!posts.length) {
    const hasMoments = m.moments?.length;
    const busy = ['running', 'queued'].includes(stepOf(m, 'draft')) || ['running', 'queued'].includes(stepOf(m, 'moments'));
    box.replaceChildren(head, el('div.empty-small', {},
      busy ? el('div', {}, el('span.spinner.xs'), ' The agent is writing these now…')
        : el('div', { text: hasMoments ? 'No drafts yet for this footage.' : 'Drafts are written from moments: the agent picks the moments, then writes a post for each.' }),
      busy ? null : el('button.btn.sm.accent', { text: hasMoments ? '✎ Write posts' : '✦ Find moments & write posts', onclick: () => runAI(m, hasMoments ? 'draft' : 'moments') })));
    return;
  }
  const mo = new Map((m.moments || []).map(x => [x.n, x]));
  const match = matchingPost(m, c, posts);
  let matchNode = null;
  const cards = posts.map(p => {
    const moment = mo.get(p.n);
    const key = moment ? momentKey(moment) : `n${p.n}`;
    const card = el('div.post' + (p.n === match ? '.match' : ''), {},
      el('div.post-head', {}, el('span.m-n', { text: p.n }), el('span.post-title', { text: moment?.title || p.title || `Post ${p.n}` }),
        p.n === match ? el('span.tag', { text: 'this clip' }) : null,
        moment ? el('button.btn.xs.ghost', { text: '▶', 'data-tip': 'Watch this moment', onclick: () => player.previewSource(m.id, moment.start, moment.end) }) : null),
      ...FIELDS.filter(([f]) => p[f]).map(([f, label, tip, limit]) => draftField(m, key, p.n, f, label, tip, limit, p[f])));
    if (p.n === match) matchNode = card;
    return card;
  });
  const extra = [];
  if (dr.newsletter) extra.push(el('div.post', {}, el('div.post-head', {}, el('span.post-title', { text: 'Newsletter paragraph' })),
    draftField(m, 'nl', 'nl', 'text', 'Newsletter', 'For your email list', null, dr.newsletter)));
  if (dr.ideas?.length) extra.push(el('div.post', {}, el('div.post-head', {}, el('span.post-title', { text: 'Ideas for next time' })),
    el('ul.ideas', {}, dr.ideas.map(i => el('li', { text: i })))));
  box.replaceChildren(head, ...cards, ...extra);
  for (const ta of $$('textarea.draft', box)) autoGrow(ta);
  if (matchNode) requestAnimationFrame(() => matchNode.scrollIntoView({ block: 'start', behavior: 'smooth' }));
}
function stepOf(m, s) {
  const jobs = Object.values(S.server?.jobs || {}).concat(S.server?.queued_jobs || []);
  if (m.steps?.[s]) return 'done';
  return jobs.some(j => j.kind === 'steps' && (j.ids || []).includes(m.id) && (j.steps || []).includes(s)) ? 'running' : 'todo';
}

function draftField(m, key, n, f, label, tip, limit, original) {
  const mid = m.id, lk = `${mid}|${key}|${f}`;
  const legacy = localStorage.getItem(pkey(mid, n, f));
  const server = m.draft_edits?.[key]?.[f];
  let saved = localEdits.has(lk) ? localEdits.get(lk) : server ?? null;
  if (saved == null && legacy != null && legacy !== original) {   // one-time move to the log
    saved = legacy;
    api.saveDraft(mid, { key, field: f, n, text: legacy }).then(() => localStorage.removeItem(pkey(mid, n, f))).catch(() => {});
  }
  const ta = el('textarea.draft', { spellcheck: 'true', rows: 2 });
  ta.value = saved ?? original;
  const count = el('span.count');
  const reset = el('button.btn.xs.ghost', { text: 'Reset', 'data-tip': 'Go back to the agent’s draft', hidden: saved == null || saved === original });
  const edited = el('span.edited-tag', { text: 'your version', hidden: saved == null || saved === original });
  const upd = () => {
    const len = ta.value.length;
    count.textContent = limit ? `${len}/${limit}` : `${len}`;
    count.classList.toggle('over', !!limit && len > limit);
  };
  const push = debounce(() => {
    const text = ta.value === original ? null : ta.value;
    api.saveDraft(mid, { key, field: f, n, text }).catch(e => toast('Couldn’t save your rewrite: ' + e.message, { kind: 'error' }));
  }, 500);
  const mark = () => { reset.hidden = edited.hidden = ta.value === original; };
  // the post as the agent drafted it, kept as is or rewritten: one choice each time
  const choice = action => S.slug && api.logChoice({ project: S.slug, kind: 'draft', action, shown: [`${mid}@${n}:${f}`],
    chosen: `${mid}@${n}:${f}`, ...(action === 'rewrite' ? { text: ta.value } : {}),
    options: { [`${mid}@${n}:${f}`]: { original } } });
  let dirtyField = false;
  ta.addEventListener('input', () => { dirtyField = true; localEdits.set(lk, ta.value === original ? null : ta.value); upd(); autoGrow(ta); mark(); push(); });
  ta.addEventListener('blur', () => { if (dirtyField) { dirtyField = false; push.flush(); if (ta.value !== original) choice('rewrite'); } });
  ta.addEventListener('keydown', e => e.stopPropagation());
  reset.onclick = () => { ta.value = original; localEdits.set(lk, null); mark(); upd(); autoGrow(ta); push.flush(); };
  upd();
  return el('div.dfield', {},
    el('div.dfield-head', {}, el('span.dlabel', { text: label, 'data-tip': tip }), edited, count, reset,
      el('button.btn.xs', { text: 'Copy', onclick: () => { copyText(ta.value); emit('posted'); choice(ta.value === original ? 'keep' : 'rewrite'); } })),
    ta);
}
function autoGrow(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, 420) + 'px'; }

/* =====================================================================
   ACTIVITY TAB
   ===================================================================== */
const who = a => a === 'operator' ? 'You' : a === 'agent' ? 'Agent' : a === 'cli' ? 'Terminal' : 'Studio';
function mname(id) { return S.server?.media?.[id]?.name || (id ? id.slice(0, 6) : ''); }
const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
function pname(slug) { return S.server?.projects?.[slug]?.name || slug; }
export function describe(ev) {
  const d = ev.data || {}, m = mname(ev.media);
  switch (ev.type) {
    case 'timeline.saved': {
      const what = ev._diff;
      return what ? `${what} in “${pname(d.project)}”` : `edited the story “${pname(d.project)}” (${plural(d.timeline?.clips?.length ?? 0, 'clip')})`;
    }
    case 'render.done': return `exported “${pname(d.project)}” · ${fmtDur(d.duration)} ${d.aspect}`;
    case 'project.created': return `started the project “${d.name}”`;
    case 'ingested': return `added ${d.name}`;
    case 'media.moved': return `moved ${m} to “${pname(d.project)}”`;
    case 'note': return isAgent(d.actor) ? `replied: “${String(d.text || '').slice(0, 160)}”` : `left a note: “${String(d.text || '').slice(0, 160)}”`;
    case 'note.ack': return `saw ${plural((d.ids || []).length, 'note')}`;
    case 'draft.edited': return `rewrote a ${d.field === 'x' ? 'X post' : d.field === 'linkedin' ? 'LinkedIn post' : d.field === 'short' ? 'caption' : d.field === 'title' ? 'title' : 'draft'} for ${m}`;
    case 'job.started': return d.kind === 'upload' ? `started preparing ${d.name}` : d.kind === 'render' ? `started exporting “${pname(d.project)}”` : `started ${(d.steps || []).join(', ')}`;
    case 'job.done': return `finished ${d.kind === 'steps' ? 'a task' : d.kind}`;
    case 'job.failed': return `couldn’t finish ${d.kind}: ${String(d.error || '').split('\n')[0].slice(0, 120)}`;
    case 'step.failed': return `couldn’t ${d.step} ${m}`;
    case 'step.transcribe': return d.skipped ? `${m}: no sound — nothing to write out` : `wrote out ${plural(d.words ?? 0, 'word')} of ${m}`;
    case 'step.proxy': return d.skipped ? `${m}: audio only — no preview needed` : `made a preview of ${m}`;
    case 'step.speech': return d.skipped ? `${m}: no sound — no speech to find` : `found ${plural(d.regions ?? 0, 'stretch', 'stretches')} of speech in ${m}`;
    case 'exception.granted': return `let “${pname(d.project)}” break the ${d.rule} rule${d.clip ? ' on one clip' : ''}: ${String(d.why || '').slice(0, 140)}`;
    case 'exception.revoked': return `took back an exception (${d.rule}) in “${pname(d.project)}”${d.why ? `: ${String(d.why).slice(0, 140)}` : ''}`;
    case 'choice': return `chose ${d.chosen ? `${d.kind} ${d.position + 1} of ${(d.shown || []).length}` : `none of ${(d.shown || []).length}`}${d.action !== 'pick' ? ` (${d.action})` : ''}${d.blind ? ' blind' : ''}`;
    case 'blind.round': return `set up a blind A/B of ${Object.keys(d.labels || {}).length} versions of “${pname(d.project)}”`;
    case 'check.done': return d.ok ? `checked “${pname(d.project)}”: it clears the bar` : `checked “${pname(d.project)}”: ${(d.failed || []).join(', ')} to fix`;
    case 'step.keyframes': return d.skipped ? `${m}: audio only — no frames to grab` : `grabbed ${plural(d.frames ?? 0, 'frame')} from ${m}`;
    case 'step.moments': return `found ${plural(d.moments ?? 0, 'moment')} in ${m}`;
    case 'step.draft': return `wrote ${plural(d.posts ?? 0, 'post draft')} for ${m}`;
    case 'step.cut': return `cut ${plural(d.clips ?? 0, 'clip')} from ${m}`;
    case 'brand.set': return `set brand ${d.brand} on ${m}`;
    case 'ui.started': return 'opened the studio';
    case 'bundle.imported': return `imported ${plural((d.files || []).length, 'file')} shared in “${d.bundle}”`;
    default: return ev.type + (m ? ` ${m}` : '');
  }
}
function actorOf(ev) {
  const d = ev.data || {};
  if (d.actor) return isAgent(d.actor) ? 'agent' : d.actor;
  if (ev.type.startsWith('step.') || ev.type.startsWith('job.') || ev.type === 'ingested' || ev.type === 'brand.set' || ev.type === 'ui.started') {
    if (ev.type === 'step.moments' || ev.type === 'step.draft') return 'agent';
    return 'studio';
  }
  return 'cli';
}
let actFilter = 'all';
function renderActivity() {
  const box = $('#tab-activity');
  // what each story save changed, against the previous save of the same project
  const lastTl = {};
  for (const ev of activity) {
    if (ev.type !== 'timeline.saved' || !ev.data?.project) continue;
    const before = lastTl[ev.data.project];
    if (before && ev._diff === undefined) {
      ev._diff = diffStory(before, ev.data.timeline, c => c.label || mname(c.media) || 'a clip').summary || '';
    }
    lastTl[ev.data.project] = ev.data.timeline;
  }
  const latestSave = [...activity].reverse().find(ev => ev.type === 'timeline.saved' && ev.data?.project === S.slug);
  const evs = activity.filter(ev => !['job.done', 'brand.set', 'job.started', 'note.ack'].includes(ev.type));
  const list = evs.slice().reverse().filter(ev => {
    const a = actorOf(ev);
    return actFilter === 'all' || (actFilter === 'agent' ? a === 'agent' : a === 'operator');
  }).slice(0, 300);
  // fold runs of the same edit by the same person ("edited the story ×6")
  const folded = [];
  for (const ev of list) {
    const last = folded.at(-1);
    if (last && ev.type === 'timeline.saved' && last.ev.type === 'timeline.saved' && actorOf(last.ev) === actorOf(ev) && last.ev.data?.project === ev.data?.project) last.n++;
    else folded.push({ ev, n: 1 });
  }
  const seg = el('div.seg-ctl', {}, ['all', 'agent', 'operator'].map(f =>
    el('button' + (f === actFilter ? '.on' : ''), { text: f === 'all' ? 'Everyone' : f === 'agent' ? 'Agent' : 'You', onclick: () => { actFilter = f; renderActivity(); } })));
  box.replaceChildren(
    el('div.insp-head', {}, el('div.insp-kicker', { text: 'Activity' }), el('div.hint', { text: 'Everything you and the agent do lands here, newest first.' }), seg),
    folded.length ? el('ul.activity', {}, folded.slice(0, 150).map(({ ev, n }) => {
      const a = actorOf(ev);
      const restorable = ev.type === 'timeline.saved' && ev.data?.project === S.slug && ev !== latestSave && ev.data?.timeline;
      return el('li.act.' + a, {},
        el('span.who.' + a, { text: who(a) }),
        el('span.what', { text: describe(ev) + (n > 1 ? ` · ${n} edits` : '') }),
        restorable ? el('button.btn.xs.ghost.restore', { text: 'Restore', 'data-tip': 'Put the story back the way it was right after this edit (Undo brings back the current one)', onclick: () => restoreVersion(ev) }) : null,
        el('span.when', { text: timeAgo(ev.ts), title: ev.ts }));
    })) : el('div.empty-small', { text: 'Nothing yet.' }));
}

function restoreVersion(ev) {
  const t = ev.data.timeline;
  const tl = { aspect: t.aspect || '9:16', captions: t.captions !== false, clips: (t.clips || []).map(c => ({ ...c, in: +c.in, out: +c.out })) };
  commit(tl, 'Restore earlier version');
  toast(`Story restored to how it was ${timeAgo(ev.ts)}.`, { kind: 'good', actions: [{ label: 'Undo', run: () => emit('undo') }] });
}

/* =====================================================================
   TELL THE AGENT TAB
   ===================================================================== */
let draftNote = localStorage.getItem('studio.note') || '';
function focusSummary() {
  const f = focusPayload();
  const bits = [`Project: ${f.project}`];
  if (f.selection) bits.push(`Clip ${f.selection.index + 1}: ${f.selection.label || f.selection.name} (${fmtT(f.selection.in)}–${fmtT(f.selection.out)})`);
  else if (f.media) bits.push(`Footage: ${f.media.name}`);
  bits.push(`Playhead: ${f.playhead_hms}`);
  if (f.transcript_selection) bits.push(`Words: “${f.transcript_selection.text.slice(0, 80)}${f.transcript_selection.text.length > 80 ? '…' : ''}”`);
  return bits;
}
function renderAgent() {
  const box = $('#tab-agent');
  const ta = el('textarea.note', { rows: 5, placeholder: 'e.g. “Make this clip start on ‘With Jefe’” or “Cut a 30-second version for LinkedIn from my two best moments.”' });
  ta.value = draftNote;
  ta.addEventListener('input', () => { draftNote = ta.value; localStorage.setItem('studio.note', draftNote); });
  const send = async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    try {
      sendFocus();
      await api.sendNote(text, S.slug, focusPayload());
      draftNote = ''; localStorage.removeItem('studio.note');
      toast('Sent to the agent.', { kind: 'good' });
      setTimeout(renderAgent, 300);
    } catch (e) { toast('Couldn’t send: ' + e.message, { kind: 'error' }); }
  };
  ta.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send(); });
  const notes = (S.server?.notes || []).filter(n => !n.project || n.project === S.slug).slice(-40);
  const byId = new Map(notes.map(n => [n.id, n]));
  const thread = notes.map(n => {
    const mine = !isAgent(n.actor);
    const re = n.re && byId.get(n.re);
    return el('li.msg.' + (mine ? 'me' : 'agent'), {},
      re && !mine ? el('div.msg-re', { text: `re: “${String(re.text).slice(0, 60)}${re.text.length > 60 ? '…' : ''}”` }) : null,
      el('div.note-text', { text: n.text }),
      el('div.note-meta', {}, mine ? 'You' : '✦ Agent', ' · ', timeAgo(n.ts),
        mine ? el('span.ack' + (n.acked ? '.on' : ''), { text: n.acked ? ' · ✓ the agent saw this' : ' · sent', 'data-tip': n.acked ? 'The agent has read this note' : 'Waiting for the agent to read it' }) : null));
  });
  const parts = [
    el('div.insp-head', {}, el('div.insp-kicker', { text: 'Tell the agent' }),
      el('div.hint', {}, 'Your agent is working on this same project from the terminal. Leave a note about what you want; it’s sent along with what you have selected right now.')),
    el('div.focus-card', {}, el('div.fc-title', { text: 'The agent will see' }), el('ul', {}, focusSummary().map(b => el('li', { text: b })))),
    notes.length ? el('div.sec-head', {}, el('h3', { text: 'Conversation' })) : null,
    notes.length ? el('ul.notes.thread', {}, thread) : null,
    ta,
    el('div.note-actions', {}, el('span.hint', { text: '⌘↩ to send' }), el('button.btn.primary', { text: 'Send to the agent', onclick: send })),
    el('div.hint.mono', {}, 'The agent reads notes with ', el('code', { text: 'studio notes' }), ', your selection with ', el('code', { text: 'studio focus' }), ' and answers with ', el('code', { text: 'studio say' }), '.'),
  ];
  box.replaceChildren(...parts.filter(Boolean));
  const th = box.querySelector('.thread');
  if (th) th.scrollTop = th.scrollHeight;
}

/* ===================================================================== */
export function initInspector() {
  for (const b of $$('#inspector .tab')) b.addEventListener('click', () => openTab(b.dataset.tab));
  on('openTab', openTab);
  on('postsForStory', () => {
    const c = S.tl.clips.find(x => mediaOf(x.media)?.drafts?.posts?.length) || S.tl.clips[0];
    if (c) select(c.id);
    openTab('posts');
  });
  window.addEventListener('pointerup', wordUp);
  on('select', () => {
    if (tab === 'clip') { if (!C || C.clipId !== S.sel || (!S.sel && C.mid !== S.media)) renderClip(); else updateClip(); }
    else if (tab === 'posts') renderPosts();
    else if (tab === 'agent') renderAgent();
  });
  on('media', () => {
    if (tab === 'clip' && !S.sel && (!C || C.mid !== S.media)) renderClip();
    if (tab === 'posts') renderPosts();
  });
  on('timeline', d => {
    if (tab !== 'clip') return;
    if (C && C.clipId && !selClip()) return renderClip();
    if (!d?.preview && C && selClip() && C.clipId === S.sel) {
      // label/index text may have changed
      const k = $('#tab-clip .insp-kicker'); const c = selClip();
      if (k && c) k.textContent = `Clip ${selIndex() + 1} of ${S.tl.clips.length} · ${mediaOf(c.media)?.name || ''}`;
    }
    updateClip();
  });
  on('playhead', () => { if (tab === 'clip') updateNowBatched(); if (tab === 'agent' && !S.playing) agentSoon(); });
  on('project', () => { C = null; renderTab(); });
  let postsSig = '', ctaSig = '';
  on('server', () => {
    if (tab === 'clip' && !C) {   // the "find moments" call-to-action follows the footage
      const sig = needsMoments().map(m => m.id).join();
      if (sig !== ctaSig) { ctaSig = sig; renderClip(); }
    }
    if (tab === 'clip' && C) {   // words (or keyframes) may have just arrived
      const m = mediaOf(C.mid);
      if (m && transcriptStamp(m) !== C.stamp) renderClip();
    }
    if (tab === 'posts') {
      const m = mediaOf(selClip()?.media || S.media);
      const sig = JSON.stringify([m?.id, m?.drafts, m?.moments?.length, m?.steps?.draft]);
      if (sig !== postsSig) { postsSig = sig; renderPosts(); }
    }
    if (tab === 'agent' && document.activeElement?.tagName !== 'TEXTAREA') renderAgent();
  });
  on('activity', ev => {
    if (tab === 'activity') renderActivitySoon();
    else if (actorOf(ev) === 'agent') $('#activity-dot').hidden = false;
  });
  renderTab();
}
const updateNowBatched = rafBatch(updateNow);
const renderActivitySoon = debounce(() => renderActivity(), 200);
const agentSoon = debounce(() => { if (document.activeElement?.tagName !== 'TEXTAREA') renderAgent(); }, 500);
