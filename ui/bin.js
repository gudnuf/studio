// Left pane: the project's footage, its AI moments, and finished exports.

import { S, on, emit, focusMedia, mediaOf, projectMedia, project } from './store.js';
import * as api from './api.js';
import { player } from './player.js';
import { addClip, DND_TYPE } from './timeline.js';
import { $, el, fmtT, fmtDur, toast, timeAgo, modal, isAgent } from './util.js';
import { native } from './native.js';
import { refresh } from './live.js';

const STEP_LABEL = { proxy: 'Preview', transcribe: 'Words', keyframes: 'Frames', moments: 'Moments', draft: 'Posts' };
const STEP_TIP = {
  proxy: 'A light copy of the video for smooth playback here',
  transcribe: 'Every spoken word, with timing, written out on this computer',
  keyframes: 'Still frames for thumbnails',
  moments: 'The agent’s picks for the best parts',
  draft: 'Post captions the agent wrote for these moments',
};
const SHOW_STEPS = ['proxy', 'transcribe', 'keyframes', 'moments', 'draft'];

/** Status of each step for a media item: done | running | queued | failed | skipped | todo */
export function stepStatus(m) {
  const st = {};
  const jobs = Object.values(S.server?.jobs || {}).concat(S.server?.queued_jobs || []);
  let busy = [], waiting = false;   // waiting: queued behind other files, nothing started
  for (const j of jobs) {
    if (j.kind === 'steps' && (j.ids || []).includes(m.id)) busy.push(...(j.steps || []));
    if (j.kind === 'upload' && j.name === m.name) {
      busy.push('proxy', 'transcribe', 'keyframes');
      if (j.progress?.step === 'waiting' && !Object.keys(j.parts || {}).length) waiting = true;
    }
  }
  const prog = S.server?.progress?.[m.id];
  let firstBusy = true;
  for (const s of SHOW_STEPS) {
    const d = m.steps?.[s];
    if (d) st[s] = d.skipped ? 'skipped' : 'done';
    else if (m.error?.step === s) st[s] = 'failed';
    else if (prog?.step === s) st[s] = 'running';     // reported by the step itself (also for CLI runs)
    else if (busy.includes(s) || (s === 'moments' && busy.includes('draft'))) { st[s] = firstBusy && !waiting ? 'running' : 'queued'; firstBusy = false; }
    else st[s] = 'todo';
  }
  return st;
}
export const isBusy = m => Object.values(stepStatus(m)).some(x => x === 'running' || x === 'queued');

function thumbUrl(m) {
  const k = m.keyframes?.[Math.min(1, (m.keyframes?.length || 1) - 1)] || m.keyframes?.[0];
  const v = m.steps?.keyframes?.sha256;
  return k ? `/files/media/${m.id}/${k.file}` + (v ? `?v=${String(v).slice(0, 16)}` : '') : null;
}

/** Ask the agent for moments — and, in the same request, the post drafts that go with them
 *  (one click, one wait). step 'draft' alone is for files that already have moments. */
export async function runAI(m, step, force = false) {
  const steps = step === 'moments' ? ['moments', 'draft'] : [step];
  try {
    await api.runSteps([m.id], steps, force);
    toast(step === 'moments'
      ? `The agent is picking the best moments in “${m.name}” and drafting posts for them. About a minute — keep editing.`
      : `The agent is writing post drafts for “${m.name}”. About a minute — keep editing.`, { ms: 4500 });
    refresh();
  } catch (e) { toast('Couldn’t start that: ' + e.message, { kind: 'error' }); }
}

/** Footage in this project that can still get moments (has words or pictures, not started). */
export function needsMoments() {
  return projectMedia().filter(m => {
    const st = stepStatus(m);
    return (st.moments === 'todo' || st.moments === 'failed') && !isBusy(m) &&
      (m.steps?.transcribe || m.steps?.keyframes);
  });
}
export async function runAIAll() {
  const ms = needsMoments();
  if (!ms.length) return;
  try {
    const r = await api.runSteps(ms.map(m => m.id), ['moments', 'draft']);
    if (r?.job) S.findAll = { job: r.job, ids: ms.map(m => m.id) };
    toast(`The agent is going through ${ms.length} file${ms.length > 1 ? 's' : ''} at once: moments first, then post drafts. A minute or two — keep editing.`, { ms: 5000 });
    refresh();
  } catch (e) { toast('Couldn’t start that: ' + e.message, { kind: 'error' }); }
}

/** Files of a "find moments" run that are finished (drafted, or given up on). */
export function filesDone(ids, steps = ['moments', 'draft']) {
  return ids.filter(id => {
    const m = mediaOf(id);
    return m && (steps.every(s => m.steps?.[s]) || steps.includes(m.error?.step));
  }).length;
}

/** The "Find moments in all" job ended: say where the moments are and open the first file. */
function findAllDone(ev) {
  const run = S.findAll;
  S.findAll = null;
  lastSig = '';
  if (ev.type === 'job.failed') { renderBin(); return; }   // live.js already toasted the error
  const order = projectMedia().sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  const found = order.filter(m => run.ids.includes(m.id) && m.moments?.length);
  if (!found.length) {
    toast('The agent didn’t find moments worth cutting in those files. Drag footage onto the Story and trim it yourself.', { kind: 'warn', ms: 7000 });
    renderBin();
    return;
  }
  focusMedia(found[0].id);
  requestAnimationFrame(showMoments);
  toast(`Moments found in ${found.length} file${found.length > 1 ? 's' : ''}. Click a file to see them.`, { kind: 'good', ms: 8000 });
  native?.notify('Moments found', `The agent picked moments in ${found.length} file${found.length > 1 ? 's' : ''} and drafted the posts.`);
}

function confirmFindAgain(m) {
  modal((card, close) => {
    const n = m.moments?.length || 0;
    card.append(el('div.modal-head', {}, el('h2', { text: 'Find new moments?' })),
      el('p.hint', { text: `This replaces ${n === 1 ? 'this moment' : `these ${n} moments`} (and their post drafts) with a fresh set from the agent. It takes about a minute. Clips already in your Story stay where they are.` }),
      el('div.modal-actions', {}, el('button.btn', { text: 'Keep these', onclick: close }),
        el('button.btn.primary', { text: 'Find new moments', onclick: () => { close(); runAI(m, 'moments', true); } })));
  });
}

export function showMoments() {
  const box = $('#moments');
  if (box && !box.hidden) box.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function chip(step, status, m) {
  const tipState = { done: 'done', running: 'working on it now', queued: 'waiting its turn', failed: 'failed — ' + (m.error?.error || ''), skipped: 'not needed for this file', todo: 'not yet' }[status];
  const pct = status === 'running' && S.server?.progress?.[m.id]?.step === step ? S.server.progress[m.id].pct : null;
  return el('span.chip.' + status, { 'data-tip': `${STEP_LABEL[step]}: ${tipState}. ${STEP_TIP[step]}`, dataset: { step } },
    status === 'running' ? el('span.spinner.xs') : null,
    status === 'done' ? el('span.ck', { text: '✓' }) : null,
    STEP_LABEL[step],
    status === 'running' ? el('span.pct', { text: pct != null ? ` ${Math.round(pct)}%` : '' }) : null);
}

function mediaCard(m, inProject = true) {
  const st = stepStatus(m);
  const url = thumbUrl(m);
  const selected = S.media === m.id;
  const portrait = (m.height || 0) > (m.width || 0);
  const card = el('div.media-card' + (selected ? '.selected' : ''), {
    draggable: m.has_video ? 'true' : 'false',
    dataset: { id: m.id },
    'data-tip': m.has_video ? 'Click to see its moments and words. Drag onto the Story to add the whole clip.' : 'Audio only',
  },
  el('div.thumb' + (portrait ? '.portrait' : ''), {},
    url ? el('img', { src: url, loading: 'lazy', decoding: 'async', draggable: 'false', alt: '' }) : el('div.thumb-ph', { text: m.has_video ? '…' : '♪' }),
    el('span.dur', { text: fmtT(m.duration, 0) })),
  el('div.info', {},
    el('div.name', { text: m.name, title: m.name }),
    el('div.chips', {}, SHOW_STEPS.filter(s => st[s] !== 'skipped').map(s => chip(s, st[s], m))),
    m.error ? el('div.err', { text: `Couldn’t do “${STEP_LABEL[m.error.step] || m.error.step}”. `, 'data-tip': m.error.error },
      el('button.linkbtn', { text: 'Try again', onclick: e => { e.stopPropagation(); runAI(m, m.error.step, true); } })) : null,
    el('div.card-actions', {},
      inProject ? null : el('button.btn.xs', { text: 'Use in this project', onclick: e => { e.stopPropagation(); api.moveMedia(m.id, S.slug).then(refresh); } }),
      m.has_video ? el('button.btn.xs.ghost', { text: '▶ Preview', 'data-tip': 'Watch the raw footage', onclick: e => { e.stopPropagation(); focusMedia(m.id); player.previewSource(m.id); } }) : null,
      m.has_video ? el('button.btn.xs.ghost', { text: '+ Story', 'data-tip': 'Add the whole clip to the end of your story', onclick: e => { e.stopPropagation(); addClip({ media: m.id, in: 0, out: m.duration }); } }) : null,
      st.moments === 'todo' || st.moments === 'failed'
        ? el('button.btn.xs.accent', { text: '✦ Find moments', 'data-tip': 'The agent watches this, picks the best parts to post and drafts the posts (about a minute)', onclick: e => { e.stopPropagation(); runAI(m, 'moments'); } }) : null,
      st.moments === 'done' && st.draft === 'todo'
        ? el('button.btn.xs.accent', { text: '✎ Write posts', 'data-tip': 'The agent drafts captions for X, LinkedIn and Shorts from these moments', onclick: e => { e.stopPropagation(); runAI(m, 'draft'); } }) : null)));
  card.addEventListener('click', () => { focusMedia(m.id); requestAnimationFrame(showMoments); });
  card.addEventListener('dragstart', e => {
    e.dataTransfer.setData(DND_TYPE, JSON.stringify({ media: m.id, in: 0, out: m.duration }));
    e.dataTransfer.effectAllowed = 'copy';
    document.body.classList.add('dragging-in');
  });
  card.addEventListener('dragend', () => document.body.classList.remove('dragging-in'));
  return card;
}

/** Is this moment in the story? A clip added from it (even trimmed since), or a clip that
 *  covers most of it. */
function inStory(m, mo) {
  return S.tl.clips.some(c => {
    if (c.media !== m.id) return false;
    const o = Math.min(c.out, mo.end) - Math.max(c.in, mo.start);
    return c.moment === mo.n ? o > 0 : o >= 0.8 * (mo.end - mo.start);
  });
}

function momentCard(m, mo) {
  const used = inStory(m, mo);
  const d = mo.end - mo.start;
  const why = el('div.why', { text: mo.why || '' });
  const card = el('div.moment' + (used ? '.used' : ''), { draggable: 'true', 'data-tip': 'Drag onto the Story, or press Add to story' },
    el('div.m-top', {},
      el('span.m-n', { text: mo.n }),
      el('span.m-title', { text: mo.title || 'Moment' }),
      el('span.m-dur', { text: fmtDur(d) })),
    mo.hook ? el('div.hook', { text: `“${mo.hook}”` }) : null,
    why,
    el('div.m-tags', {},
      mo.pillar ? el('span.tag', { text: mo.pillar, 'data-tip': 'Which brand theme this fits' }) : null,
      mo.kind ? el('span.tag.dim', { text: mo.kind }) : null,
      el('span.tag.dim', { text: `${fmtT(mo.start)}–${fmtT(mo.end)}` })),
    el('div.m-actions', {},
      el('button.btn.xs.ghost', { text: '▶ Preview', onclick: e => { e.stopPropagation(); player.previewSource(m.id, mo.start, mo.end); } }),
      used ? el('span.used-tag', { text: '✓ In story' }) : null,
      el('button.btn.xs.primary', { text: used ? '+ Add again' : '+ Add to story', onclick: e => { e.stopPropagation(); addClip({ media: m.id, in: mo.start, out: mo.end, label: mo.title, moment: mo.n }); } })));
  why.addEventListener('click', () => why.classList.toggle('open'));
  card.addEventListener('dragstart', e => {
    e.dataTransfer.setData(DND_TYPE, JSON.stringify({ media: m.id, in: mo.start, out: mo.end, label: mo.title, moment: mo.n }));
    e.dataTransfer.effectAllowed = 'copy';
    document.body.classList.add('dragging-in');
  });
  card.addEventListener('dragend', () => document.body.classList.remove('dragging-in'));
  return card;
}

// The moments panel can be folded away; that only hides it, the moments stay saved.
// Remembered across reloads so it stays out of the way until asked for.
let momentsClosed = localStorage.getItem('studio.momentsClosed') === '1';
function setMomentsClosed(v) {
  momentsClosed = v;
  localStorage.setItem('studio.momentsClosed', v ? '1' : '0');
  lastSig = ''; renderBin();
}

function renderMoments() {
  const box = $('#moments');
  const m = mediaOf(S.media);
  if (!m) { box.replaceChildren(); box.hidden = true; return; }
  box.hidden = false;
  box.classList.toggle('closed', momentsClosed);
  if (momentsClosed) {
    const n = m.moments?.length || 0;
    box.replaceChildren(el('div.sec-head.folded', { onclick: () => setMomentsClosed(false), 'data-tip': 'Show the agent’s moments for this footage' },
      el('h3', { text: 'Moments' }), el('span.sec-sub', { text: n ? `${n} saved · ${m.name}` : m.name }),
      el('button.btn.xs.ghost', { text: 'Show ▾' })));
    return;
  }
  const st = stepStatus(m);
  const head = el('div.sec-head', {}, el('h3', { text: 'Moments' }), el('span.sec-sub', { text: m.name }));
  const closeBtn = el('button.btn.xs.ghost.close-moments', { text: 'Hide ▴', 'data-tip': 'Fold the moments away. They stay saved: press Show to bring them back.', onclick: () => setMomentsClosed(true) });
  let body;
  if (m.moments?.length) {
    body = el('div.moment-list', {}, m.moments.map(mo => momentCard(m, mo)));
    head.append(el('button.btn.xs.ghost', { text: 'Find again', 'data-tip': 'Ask the agent for a fresh set of moments (replaces these)', onclick: () => confirmFindAgain(m) }));
  } else if (st.moments === 'running' || st.moments === 'queued') {
    body = el('div.empty-small', {}, el('span.spinner'), ' The agent is watching this footage and picking moments…');
  } else if (!m.steps?.transcribe && m.has_audio || !m.steps?.keyframes && m.has_video) {
    body = el('div.empty-small', {}, el('span.spinner'), ' Getting this footage ready (words and frames)… then the agent can pick moments.');
  } else if (st.moments === 'done') {
    body = el('div.empty-small', {},
      el('div', { text: 'The agent didn’t find anything worth cutting here. That’s normal for silent screen recordings: drag it onto the Story and trim it yourself.' }),
      el('button.btn.sm.ghost', { text: 'Ask again', onclick: () => runAI(m, 'moments', true) }));
  } else if (!m.has_video) {
    body = el('div.empty-small', { text: 'Audio only — moments still work, but it can’t go on the Story yet.' });
  } else {
    body = el('div.empty-small', {},
      el('div', { text: 'No moments yet. The agent can watch this and suggest the best parts to post.' }),
      el('button.btn.sm.accent', { text: '✦ Find moments', onclick: () => runAI(m, 'moments') }),
      el('div.hint', { text: 'Or skip it: drag the whole clip onto the Story and trim it yourself.' }));
  }
  head.append(closeBtn);
  box.replaceChildren(head, body);
}

let showAllExports = false, freshExport = null;
function renderExports() {
  const box = $('#exports');
  const ex = (project()?.exports || []).slice().reverse();
  if (!ex.length) { box.replaceChildren(); box.hidden = true; return; }
  box.hidden = false;
  const shown = showAllExports ? ex.slice(0, 20) : ex.slice(0, 2);
  box.replaceChildren(
    el('div.sec-head', {}, el('h3', { text: 'Your videos' }), el('span.sec-sub', { text: `${ex.length} exported` }),
      ex.length > 2 ? el('button.btn.xs.ghost', { text: showAllExports ? 'Fewer' : `All ${ex.length}`, onclick: () => { showAllExports = !showAllExports; lastSig = ''; renderBin(); } }) : null),
    el('div.export-list', {}, shown.map(x => el('div.export' + (x.file === freshExport ? '.fresh' : ''), {},
      el('div.ex-info', {},
        el('div.ex-name', { text: x.file.split('/').pop(), title: x.file }),
        el('div.ex-meta', { text: `${timeAgo(x.ts)} · ${fmtDur(x.duration)} · ${x.aspect}${isAgent(x.actor) ? ' · by the agent' : ''}` })),
      el('button.btn.xs.ghost', { text: '▶', 'data-tip': 'Watch it', onclick: () => playExport(x) }),
      el('button.btn.xs.accent', { text: 'Copy post', 'data-tip': 'Open the post drafts for this video’s first clip, ready to copy', onclick: () => emit('postsForStory') }),
      el('button.btn.xs.ghost', { text: 'Finder', 'data-tip': 'Show the file in Finder (AirDrop it to your phone from there)', onclick: () => api.reveal(x.file).catch(e => toast(e.message)) })))));
}
/** A render just finished: show it at the top of the left pane. */
export function showExport(file) {
  freshExport = file; lastSig = ''; renderBin();
  requestAnimationFrame(() => $('#exports')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
  setTimeout(() => { freshExport = null; lastSig = ''; renderBin(); }, 6000);
}

export function playExport(x) {
  modal((card, close) => {
    card.classList.add('wide');
    card.append(
      el('div.modal-head', {}, el('h2', { text: x.file.split('/').pop() }), el('button.btn.icon.ghost', { text: '✕', onclick: close })),
      el('video.export-video', { src: api.fileUrl(x.file), controls: true, autoplay: true, playsinline: true }),
      el('div.modal-actions', {},
        el('span.hint', { text: 'Saved on this Mac. AirDrop it to your phone from Finder, then post.' }),
        el('button.btn', { text: 'Show in Finder', onclick: () => api.reveal(x.file) }),
        el('button.btn.primary', { text: 'Copy the post', onclick: () => { close(); emit('postsForStory'); } })));
  });
}

let lastSig = '';
function renderBin() {
  const list = $('#bin-list');
  const mine = projectMedia().sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  const mineIds = new Set(mine.map(m => m.id));
  const others = Object.values(S.server?.media || {}).filter(m => !mineIds.has(m.id));
  const sig = JSON.stringify([S.slug, S.media, S.tl.clips.map(c => [c.media, c.in, c.out, c.moment]), S.findAll?.job,
    mine.map(m => [m.id, m.steps, m.error, m.moments?.length, m.keyframes?.length]),
    others.map(m => m.id), Object.keys(S.server?.jobs || {}),
    Object.entries(S.server?.progress || {}).map(([k, v]) => k + v.step), showOthers, project()?.exports?.length]);
  if (sig === lastSig) return;
  lastSig = sig;
  const kids = [];
  if (!mine.length) {
    kids.push(el('div.bin-empty', {},
      el('div.big', { text: '⬆' }),
      el('div.empty-title', { text: 'Add your footage' }),
      el('div.empty-sub', { text: 'Drag videos anywhere onto this window: phone talking-head clips, screen recordings, job walks. Studio writes out the words and makes previews right here on your Mac.' }),
      el('button.btn.primary', { text: 'Choose files…', onclick: () => $('#file-input').click() })));
  } else kids.push(...mine.map(m => mediaCard(m)));
  if (others.length) {
    kids.push(el('button.others-toggle', { text: `${showOthers ? '▾' : '▸'} Footage in other projects (${others.length})`, onclick: () => { showOthers = !showOthers; lastSig = ''; renderBin(); } }));
    if (showOthers) kids.push(...others.map(m => mediaCard(m, false)));
  }
  list.replaceChildren(...kids);
  renderMoments();
  renderExports();
  $('#bin').classList.toggle('has-lower', !$('#moments').hidden || !$('#exports').hidden);
  const todo = needsMoments(), fa = $('#btn-find-all');
  if (S.findAll) {          // a run is going: the button reports it, file by file
    const n = S.findAll.ids.length;
    fa.hidden = false; fa.disabled = true;
    fa.textContent = `✦ Finding moments · ${filesDone(S.findAll.ids)} of ${n} file${n > 1 ? 's' : ''}`;
    return;
  }
  fa.disabled = false;
  fa.hidden = !todo.length;
  fa.textContent = todo.length > 1 ? `✦ Find moments in all ${todo.length}` : '✦ Find moments';
}
let showOthers = false;

/* job.progress: patch the % in place; only re-render when a step we didn't know was running starts */
function renderProgress() {
  for (const [mid, p] of Object.entries(S.server?.progress || {})) {
    const card = document.querySelector(`#bin-list .media-card[data-id="${CSS.escape(mid)}"]`);
    if (!card) continue;
    const pct = card.querySelector(`.chip.running[data-step="${p.step}"] .pct`);
    if (pct) pct.textContent = p.pct != null ? ` ${Math.round(p.pct)}%` : '';
    else if (SHOW_STEPS.includes(p.step) && !mediaOf(mid)?.steps?.[p.step]) { lastSig = ''; renderBin(); return; }
  }
}

export function initBin() {
  const re = () => renderBin();
  $('#btn-find-all').onclick = runAIAll;
  on('progress', renderProgress);
  on('server', re);
  on('media', re);
  on('timeline', d => { if (!d?.preview) re(); });
  on('project', () => { lastSig = ''; re(); });
  on('activity', ev => {
    if ((ev.type === 'job.done' || ev.type === 'job.failed') && S.findAll && ev.data?.job === S.findAll.job) {
      refresh().then(() => findAllDone(ev));    // state first, so the moments are there to show
    }
  });
}
