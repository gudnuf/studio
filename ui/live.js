// Live link: SSE events -> debounced state refetch, activity feed, job status, connection light.

import { S, on, emit, applyServer, editMark } from './store.js';
import * as api from './api.js';
import { $, el, debounce, toast, fmtDur } from './util.js';
import { native } from './native.js';

export const activity = [];      // newest last
let connected = false;

let fetching = null, again = false;
export async function refresh() {
  if (fetching) { again = true; return fetching; }
  fetching = (async () => {
    try {
      const mark = editMark();
      const st = await api.getState();
      const stale = mark !== editMark();
      applyServer(st, { initial: !S.server, mark });
      if (stale) again = true;   // our own edit raced this fetch: fetch once more to catch remote changes
      setConn(true);
    } catch (e) {
      console.warn('state fetch failed', e);
      setConn(false);
    }
  })();
  await fetching;
  fetching = null;
  if (again) { again = false; return refresh(); }
}
const refreshSoon = debounce(refresh, 120);

function setConn(ok) {
  connected = ok;
  const c = $('#conn');
  c.classList.toggle('ok', ok);
  $('#conn-label').textContent = ok ? 'Live' : 'Reconnecting…';
}

/* job.progress: patched into the state we already have (no refetch, no activity entry);
   listeners repaint progress bars on 'progress', at most once per frame. */
let progPending = false;
function onProgress(ev) {
  const d = ev.data || {}, st = S.server;
  if (!st) return;
  if (ev.media) (st.progress ||= {})[ev.media] = { step: d.step, pct: d.pct, ts: ev.ts };
  if (d.job && st.jobs?.[d.job]) {
    const j = st.jobs[d.job];
    j.progress = { step: d.step, pct: d.pct, media: ev.media || d.media_id };
    if (d.step && d.step !== 'waiting') (j.parts ||= {})[d.step] = Math.max(j.parts[d.step] || 0, d.pct || 0);
    if (ev.media || d.media_id) j.media_id = ev.media || d.media_id;
  }
  if (!progPending) {
    progPending = true;
    requestAnimationFrame(() => { progPending = false; emit('progress'); });
  }
}

function onEvent(ev) {
  const d = ev.data || {};
  if (ev.type === 'job.progress') return onProgress(ev);
  if (ev.imported) return refreshSoon();     // a bundle's history arriving: no toasts, no feed flood
  if (ev.type === 'timeline.saved' && d.project) S.lastRemoteActor[d.project] = d.actor || 'cli';
  if (ev.type !== 'job.queued') {
    activity.push(ev);
    if (activity.length > 300) activity.splice(0, activity.length - 300);
    emit('activity', ev);
  }
  if (ev.type === 'render.done' && d.project === S.slug) emit('exported', ev);
  if (ev.type === 'transcript.edited' && ev.media) {   // a word was fixed (here or by Claude): refetch
    api.dropTranscript(ev.media);
    window.dispatchEvent(new CustomEvent('studio:transcript', { detail: ev.media }));
  }
  if (ev.type === 'note' && d.actor === 'claude' && (!d.project || d.project === S.slug)) {
    toast(`Claude: ${String(d.text || '').slice(0, 220)}`, { kind: 'claude', ms: 9000, actions: [{ label: 'Open', run: () => emit('openTab', 'claude') }] });
  }
  if (ev.type === 'job.done' && d.kind === 'share' && d.result?.dir) {
    const r = d.result, gb = n => `${(n / 1e9).toFixed(n > 1e10 ? 0 : 1)} GB`;
    toast(`Shared: ${r.dir}${r.zip ? ` and ${r.zip.split('/').pop()} (${gb(r.zip_bytes)}) — upload the .zip to Google Drive` : ` (${gb(r.bytes)})`}`, { kind: 'good', ms: 12000, actions: [{ label: 'Copy path', run: () => navigator.clipboard?.writeText(r.zip || r.dir) }] });
    if (r.zip_bytes > 15e9) toast(`The zip is ${gb(r.zip_bytes)}: Google Drive takes it, but uploading and downloading will be slow. One project per bundle keeps them smaller.`, { kind: 'warn', ms: 10000 });
    native?.notify('Bundle ready', r.zip || r.dir);
  }
  if (ev.type === 'bundle.imported') {
    const n = (d.files || []).length, slugs = [...new Set(Object.values(d.projects || {}))];
    const names = slugs.map(s => S.server?.projects?.[s]?.name || s);
    toast(`Imported ${n} file${n === 1 ? '' : 's'}${names.length ? ` into ${names.map(x => `“${x}”`).join(', ')}` : ''}. Previews are being made.${(d.renamed || []).length ? ` (You already had a different “${d.renamed[0].from}”, so it came in as “${d.renamed[0].to}”.)` : ''}`,
      { kind: 'good', ms: 9000, actions: slugs.length ? [{ label: 'Open', run: () => emit('openProject', slugs[0]) }] : [] });
  }
  if (ev.type === 'job.failed') {
    toast(`Something didn’t finish: ${String(d.error || '').split('\n')[0].slice(0, 140)}`, { kind: 'error', ms: 6000 });
  }
  if (ev.type === 'step.failed') {
    const m = S.server?.media?.[ev.media];
    toast(`Couldn’t finish “${d.step}” for ${m?.name || 'a file'}.`, { kind: 'error', ms: 5000 });
  }
  if ((ev.type === 'step.moments' || ev.type === 'step.draft') && !S.findAll?.ids?.includes(ev.media)) {
    // (during "Find moments in all" one toast at the end says where to look, not one per file)
    const m = S.server?.media?.[ev.media];
    toast(ev.type === 'step.moments' ? `Claude found ${d.moments ?? 'some'} moments in ${m?.name || 'your footage'}` : `Post drafts are ready for ${m?.name || 'your footage'} — see the Posts tab`, { kind: 'good', ms: 4500 });
    if (ev.type === 'step.moments') native?.notify('Moments found', `Claude found ${d.moments ?? 'some'} moments in ${m?.name || 'your footage'}.`);
  }
  refreshSoon();
}

/* ---- jobs indicator in the footer ---- */
const STAGE = { proxy: 'making the preview', transcribe: 'writing out the words', keyframes: 'grabbing frames' };
const STAGE_W = { proxy: 50, transcribe: 40, keyframes: 10 };
/** One rising % for an upload: proxy 50 / words 40 / frames 10, finished steps count fully. */
function uploadStatus(j) {
  const m = j.media_id ? S.server?.media?.[j.media_id] : Object.values(S.server?.media || {}).find(x => x.name === j.name);
  const parts = j.parts || {};
  if (!m) return { label: 'copying in', pct: null };
  let tot = 0, got = 0, stage = null;
  for (const [s, w] of Object.entries(STAGE_W)) {
    const st = m.steps?.[s];
    if (st?.skipped) continue;
    tot += w;
    if (st) got += w;
    else { got += w * Math.min(100, parts[s] || 0) / 100; if (!stage && parts[s] != null) stage = s; }
  }
  const waiting = j.progress?.step === 'waiting' && !Object.keys(parts).length;
  return { label: waiting ? 'waiting for its turn' : STAGE[stage] || 'getting ready', pct: waiting || !tot ? null : 100 * got / tot };
}
const JOB_WORDS = {
  upload: j => `Preparing ${j.name || 'upload'} · ${uploadStatus(j).label}`,
  render: j => `Exporting ${j.project || ''}`,
  share: j => `Packing ${(j.projects || []).join(', ')} to share`,
  import: j => `Importing ${j.bundle || 'shared footage'}`,
  steps: j => {
    const s = (j.steps || []).join(', ');
    const ids = j.ids || [], n = ids.length;
    const words = { moments: 'Finding moments', draft: 'Writing posts', transcribe: 'Writing out the words', proxy: 'Making previews', keyframes: 'Grabbing frames', cut: 'Cutting clips' };
    const done = ids.filter(id => { const m = S.server?.media?.[id]; return m && ((j.steps || []).every(x => m.steps?.[x]) || (j.steps || []).includes(m.error?.step)); }).length;
    return (j.steps || []).map(x => words[x] || x).join(' + ') + (n > 1 ? ` · ${done} of ${n} files done` : '') || s;
  },
};
function renderJobs() {
  const box = $('#jobs');
  const jobs = Object.values(S.server?.jobs || {});
  const q = S.server?.queued || 0;
  if (!jobs.length && !q) { box.replaceChildren(); return; }
  box.replaceChildren(...jobs.map(j => {
    const since = (Date.now() - new Date(j.ts).getTime()) / 1000;
    const pct = j.kind === 'upload' ? uploadStatus(j).pct : j.progress?.pct;
    return el('span.job', { 'data-tip': 'Running on this Mac. You can keep editing.', dataset: { job: j.job || '' } }, el('span.spinner.xs'),
      `${(JOB_WORDS[j.kind] || (() => j.kind))(j)}`,
      el('span.job-pct', { text: pct != null ? ` ${Math.round(pct)}%` : '' }),
      el('span.job-t', { dataset: { since: j.ts }, text: isFinite(since) ? ` · ${fmtDur(since)}` : '' }));
  }), q ? el('span.job.dim', { text: (S.server?.queued_jobs || []).some(j => j.kind === 'render') ? `Export is next · +${q} waiting` : `+${q} waiting`, 'data-tip': 'Waiting for a free slot on this Mac; starts automatically' }) : '');
}
setInterval(() => {
  for (const s of document.querySelectorAll('.job-t[data-since]')) {
    const since = (Date.now() - new Date(s.dataset.since).getTime()) / 1000;
    if (isFinite(since)) s.textContent = ` · ${fmtDur(since)}`;
  }
}, 1000);

function renderJobPct() {
  for (const n of document.querySelectorAll('#jobs .job[data-job]')) {
    const j = S.server?.jobs?.[n.dataset.job];
    if (!j) continue;
    if (j.kind === 'upload') {   // label changes with the stage; re-render this one
      const nn = n.childNodes[1];
      if (nn && nn.nodeType === 3) nn.textContent = JOB_WORDS.upload(j);
    }
    const pct = j.kind === 'upload' ? uploadStatus(j).pct : j.progress?.pct;
    const t = n.querySelector('.job-pct');
    if (t) t.textContent = pct != null ? ` ${Math.round(pct)}%` : '';
  }
}

export async function initLive() {
  on('server', renderJobs);
  on('progress', renderJobPct);
  try {
    const evs = await api.getActivity();
    if (Array.isArray(evs)) activity.push(...evs);
    for (const ev of evs || []) {
      if (ev.type === 'timeline.saved' && ev.data?.project) S.lastRemoteActor[ev.data.project] = ev.data.actor || 'cli';
    }
  } catch { /* older server */ }
  await refresh();
  api.stream({ onEvent, onOpen: () => { setConn(true); refreshSoon(); }, onError: () => setConn(false) });
  // safety net: if the stream silently stalls, still pick up changes
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
}
