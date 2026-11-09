// Everything that talks to server.py. Defensive: the backend is evolving underneath us.

async function req(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  let data = null;
  const txt = await r.text();
  try { data = txt ? JSON.parse(txt) : null; } catch { data = { error: txt.slice(0, 300) }; }
  if (!r.ok || (data && data.error && !Array.isArray(data))) {
    throw new Error((data && data.error) || `${method} ${url} → ${r.status}`);
  }
  return data;
}

export const getState = () => req('GET', '/api/state');
export const getHealth = () => req('GET', '/api/health');
/** The app's drops and file picker: add files (or folders) where they are, no upload. */
export const ingestPaths = (paths, project, brand) => req('POST', '/api/ingest-paths', { paths, project, brand });
export const getActivity = () => req('GET', '/api/activity');
/** Save the story. `base` = the server revision this edit started from (0 for a project nobody
 *  has saved yet); the server answers 409 (err.conflict = {rev, actor, timeline}) if someone else
 *  (usually the agent) saved since. Always sent: a missing base would mean 0 to the server anyway. */
export async function putTimeline(slug, tl, base) {
  const q = `?base=${base ?? 0}`;
  const r = await fetch(`/api/projects/${encodeURIComponent(slug)}/timeline${q}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(tl) });
  const data = await r.json().catch(() => null);
  if (r.status === 409 && data?.conflict) { const e = new Error('stale'); e.conflict = data; throw e; }
  if (!r.ok || data?.error) throw new Error(data?.error || `PUT timeline → ${r.status}`);
  const rev = r.headers.get('X-Timeline-Rev');
  return { timeline: data, rev: rev != null ? +rev : null };
}
export const newProject = (name, brand) => req('POST', '/api/projects', { name, brand });
/** Share project…: write a bundle of these projects to `out` (a new folder), in the background. */
export const share = (projects, out, zip) => req('POST', '/api/bundle', { projects, out, zip });
/** Import…: merge a teammate's bundle folder into this library, in the background. */
export const importBundle = path => req('POST', '/api/import', { path });
export const runSteps = (ids, steps, force = false) => req('POST', '/api/run', { ids, steps, force });
export const render = slug => req('POST', `/api/projects/${encodeURIComponent(slug)}/render`);
export const sendNote = (text, project, focus) => req('POST', '/api/notes', { text, project, focus });
export const postFocus = focus => req('POST', '/api/focus', focus).catch(() => {});
export const moveMedia = (media, project) => req('POST', '/api/move', { media, project });
export const reveal = path => req('POST', '/api/reveal', { path });
/** The operator's rewrite of one post-draft field (text null = back to the agent's draft). */
export const saveDraft = (mid, { key, field, n, text }) => req('POST', `/api/drafts/${encodeURIComponent(mid)}`, { key, field, n, text });
/** Fix a misheard word: just the word at `index`, or (all) every `wrong` in every file. */
export const fixWord = (mid, { index, wrong, right, all }) => req('POST', `/api/words/${encodeURIComponent(mid)}/fix`, { index, wrong, right, all: !!all });

export const fileUrl = rel => '/files/' + rel.split('/').map(encodeURIComponent).join('/');

/* ---- transcripts: fetched once per (media, transcript receipt) ---- */
const tcache = new Map();
const tready = new Map();
export function getTranscript(mid, stamp = '') {
  const key = mid + '|' + stamp;
  if (!tcache.has(key)) {
    tcache.set(key, req('GET', `/api/transcript/${mid}`)
      .then(t => ({ segments: t?.segments || [], words: (t?.words || []).filter(w => w && w.w) }))
      .then(t => { tready.set(key, t); window.dispatchEvent(new CustomEvent('studio:transcript', { detail: mid })); return t; })
      .catch(() => { tcache.delete(key); return { segments: [], words: [] }; }));
  }
  return tcache.get(key);
}
/** A transcript changed on disk (transcript.edited): forget every cached copy of it. */
export function dropTranscript(mid) {
  for (const k of [...tcache.keys()]) if (k.startsWith(mid + '|')) tcache.delete(k);
  for (const k of [...tready.keys()]) if (k.startsWith(mid + '|')) tready.delete(k);
}
/** Synchronous peek for render paths that can't await. */
export function transcriptNow(mid, stamp = '') {
  const key = mid + '|' + stamp;
  if (tready.has(key)) return tready.get(key);
  getTranscript(mid, stamp);
  return null;
}

/* ---- live stream ---- */
export function stream({ onEvent, onOpen, onError }) {
  let es, retry = 1000;
  const connect = () => {
    es = new EventSource('/api/stream');
    es.onopen = () => { retry = 1000; onOpen?.(); };
    es.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch { /* partial line */ } };
    es.onerror = () => {
      onError?.();
      if (es.readyState === EventSource.CLOSED) { setTimeout(connect, retry); retry = Math.min(retry * 2, 10000); }
    };
  };
  connect();
  return () => es && es.close();
}

/* ---- upload with progress (raw body, as server.py expects) ---- */
export function upload(file, project, brand, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    const q = new URLSearchParams({ project, name: file.name, brand: brand || 'jefe' });
    x.open('POST', '/api/upload?' + q);
    x.setRequestHeader('Content-Type', 'application/octet-stream');
    x.upload.onprogress = e => e.lengthComputable && onProgress?.(e.loaded / e.total);
    x.onload = () => {
      if (x.status >= 200 && x.status < 300) { try { resolve(JSON.parse(x.responseText)); } catch { resolve({}); } }
      else {
        let msg = `upload failed (${x.status})`;
        try { msg = JSON.parse(x.responseText).error || msg; } catch { /* not JSON */ }
        reject(new Error(msg));
      }
    };
    x.onerror = () => reject(new Error('upload failed (network)'));
    x.send(file);
  });
}
