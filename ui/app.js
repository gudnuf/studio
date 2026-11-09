// Studio desktop editor — entry point. Wires the top bar, transport, keyboard, uploads and
// the footer; the panes live in their own modules.

import { S, on, emit, commit, undo, redo, canUndo, canRedo, undoLabel, redoLabel, setProject,
  project, projectMedia, total, setPlayhead, selClip, cliFor, saveNow, select, dirty } from './store.js';
import { setFormat, setCaptions, layout, diffStory } from './ops.js';
import * as api from './api.js';
import { initPlayer, player } from './player.js';
import { initTimeline, splitAtPlayhead, cancelGesture, fit, scrollToPlayhead, getZoom, setZoom } from './timeline.js';
import { initBin, playExport, showExport } from './bin.js';
import { initInspector, removeSelected, openTab } from './inspector.js';
import { initLive, refresh } from './live.js';
import { FORMATS, ORDER, formatIcon } from './formats.js';
import { $, $$, el, fmtT, toast, initTooltips, isTyping, copyText, modal, debounce, isAgent } from './util.js';
import { native } from './native.js';
import { shareDialog, importDialog } from './share.js';

/* ---------------- top bar ---------------- */
function renderProjects() {
  const sel = $('#project-select');
  const ps = Object.values(S.server?.projects || {}).sort((a, b) =>
    (a.slug === 'inbox') - (b.slug === 'inbox') || String(b.created || '').localeCompare(String(a.created || '')));
  const sig = JSON.stringify([S.slug, ps.map(p => [p.slug, p.name])]);
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.replaceChildren(...ps.map(p => el('option', { value: p.slug, text: p.slug === 'inbox' ? 'Inbox (unsorted)' : p.name, selected: p.slug === S.slug })));
}

function newProjectDialog() {
  modal((card, close) => {
    const name = el('input.text', { placeholder: 'e.g. Jefe — mulch job walk', autofocus: true });
    const brands = S.server?.brands?.length ? S.server.brands : ['jefe'];
    const brand = el('select', {}, brands.map(b => el('option', { value: b, text: b })));
    const go = async () => {
      const n = name.value.trim();
      if (!n) return name.focus();
      try {
        const r = await api.newProject(n, brand.value);
        close();
        await refresh();
        setProject(r.slug);
        toast(`Project “${n}” is ready. Add footage to start.`, { kind: 'good' });
      } catch (e) { toast(e.message, { kind: 'error' }); }
    };
    name.addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') go(); });
    card.append(el('div.modal-head', {}, el('h2', { text: 'New project' })),
      el('p.hint', { text: 'A project is one video (or a few versions of it): its footage, its story and its exports.' }),
      el('label.field', {}, el('span', { text: 'Name' }), name),
      el('label.field', {}, el('span', { text: 'Brand voice' }), brand),
      el('div.modal-actions', {}, el('button.btn', { text: 'Cancel', onclick: close }), el('button.btn.primary', { text: 'Create project', onclick: go })));
  });
}

/* the guided path */
const STEPS = [
  { k: 'add', label: 'Add footage', hint: 'Drag videos anywhere onto this window' },
  { k: 'moments', label: 'Pick moments', hint: 'Choose the best parts on the left' },
  { k: 'arrange', label: 'Arrange', hint: 'Drag clips in the Story to reorder and trim' },
  { k: 'format', label: 'Captions & format', hint: 'Pick where it’s going and check the captions' },
  { k: 'export', label: 'Export & post', hint: 'Make the video, then copy a post' },
];
const fmtSeen = slug => localStorage.getItem('studio.fmtseen.' + slug) === '1';
const posted = slug => localStorage.getItem('studio.posted.' + slug) === '1';
const markFmtSeen = () => { if (S.slug) localStorage.setItem('studio.fmtseen.' + S.slug, '1'); renderSteps(); };
function stepState() {
  const media = projectMedia();
  const clips = S.tl.clips.length;
  const ex = project()?.exports?.length || 0;
  const done = {
    add: media.length > 0,
    moments: media.some(m => m.moments?.length) || clips > 0,
    arrange: clips > 1 || (clips > 0 && (fmtSeen(S.slug) || ex > 0)),
    format: clips > 0 && (fmtSeen(S.slug) || ex > 0),
    export: ex > 0 && posted(S.slug),
  };
  const cur = STEPS.findIndex(s => !done[s.k]);
  return { done, cur: cur < 0 ? STEPS.length - 1 : cur, all: cur < 0 };
}
function renderSteps() {
  const ol = $('#steps');
  const { done, cur, all } = stepState();
  const sig = JSON.stringify([done, cur, all]);
  if (ol.dataset.sig === sig) return;
  ol.dataset.sig = sig;
  ol.replaceChildren(...STEPS.map((s, i) => el('li', {
    class: [done[s.k] ? 'done' : '', i === cur && !all ? 'current' : ''].join(' '),
    'data-tip': (i === cur ? 'Next: ' : '') + s.hint,
    onclick: () => goStep(s.k),
  }, el('span.num', { text: done[s.k] && (i !== cur || all) ? '✓' : String(i + 1) }), el('span.lbl', { text: s.label }))),
  el('li.step-hint', { text: all ? 'Done — post it! (Export again any time.)'
    : STEPS[cur].k === 'export' && (project()?.exports?.length) ? 'Video made — now press Copy post on it'
    : STEPS[cur].hint }));
}
function goStep(k) {
  if (k === 'add') addFootage();
  else if (k === 'moments') {
    const m = projectMedia().find(x => x.moments?.length) || projectMedia()[0];
    if (m && !S.media) emit('focusMediaReq', m.id);
    const fa = $('#btn-find-all');
    if (!fa.hidden) { fa.classList.remove('btn-hilite'); void fa.offsetWidth; fa.classList.add('btn-hilite'); }
    requestAnimationFrame(() => $('#moments')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    pulse('#bin');
  } else if (k === 'arrange') { pulse('#story'); fit(); }
  else if (k === 'format') { openFormatMenu(); }
  else if (k === 'export') { doExport(); }
}
function pulse(sel) { const n = $(sel); n.classList.remove('pulse'); void n.offsetWidth; n.classList.add('pulse'); }

/* undo / redo */
function renderHistory() {
  const u = $('#btn-undo'), r = $('#btn-redo');
  u.disabled = !canUndo(); r.disabled = !canRedo();
  const mod = navigator.platform.includes('Mac') ? '⌘' : 'Ctrl+';
  u.dataset.tip = canUndo() ? `Undo ${undoLabel().toLowerCase()} (${mod}Z)` : 'Nothing to undo';
  r.dataset.tip = canRedo() ? `Redo ${redoLabel().toLowerCase()} (⇧${mod}Z)` : 'Nothing to redo';
}
function doUndo() { const l = undo(); if (l) toast(`Undid: ${l}`); }
function doRedo() { const l = redo(); if (l) toast(`Redid: ${l}`); }

/* format & captions */
function renderFormat() {
  const f = FORMATS[S.tl.aspect] || FORMATS['9:16'];
  $('#fmt-icon').innerHTML = formatIcon(S.tl.aspect);
  $('#fmt-label').textContent = `${f.name} ${S.tl.aspect}`;
  $('#captions-toggle').checked = !!S.tl.captions;
  const tag = $('#stage-format-tag');
  if (!tag.classList.contains('source')) {
    tag.replaceChildren(...[el('span', { html: formatIcon(S.tl.aspect, 14) }), el('span', { text: `${f.name} · ${f.apps}` }),
      S.tl.captions ? el('span.cap-on', { text: 'Captions on' }) : null].filter(Boolean));
  }
}
function openFormatMenu() {
  const menu = $('#format-menu');
  menu.replaceChildren(el('div.menu-title', { text: 'Where is this video going?' }),
    ...ORDER.map(a => {
      const f = FORMATS[a];
      return el('button.menu-item' + (a === S.tl.aspect ? '.on' : ''), {
        onclick: () => {
          menu.hidden = true;
          markFmtSeen();
          if (a !== S.tl.aspect) { commit(setFormat(S.tl, a), `Format ${f.name}`); toast(`Format: ${f.name} (${a}) — the preview shows the crop.`); }
        },
      }, el('span.mi-icon', { html: formatIcon(a, 26) }),
      el('span.mi-text', {}, el('span.mi-name', { text: `${f.name} ${a}` }), el('span.mi-sub', { text: f.apps })));
    }),
    el('div.menu-foot', { text: 'Studio fills the frame by trimming the edges, the same as the export.' }));
  menu.hidden = false;
  const close = e => { if (!menu.contains(e.target) && e.target.closest('#btn-format') == null) { menu.hidden = true; document.removeEventListener('pointerdown', close, true); } };
  setTimeout(() => document.addEventListener('pointerdown', close, true));
}

/* export */
function renderExportBtn() {
  const b = $('#btn-export');
  const running = Object.values(S.server?.jobs || {}).some(j => j.kind === 'render' && j.project === S.slug);
  const waiting = !running && ((S.server?.queued_jobs || []).some(j => j.kind === 'render' && j.project === S.slug) || exportPending);
  b.disabled = running || waiting || !S.tl.clips.length;
  b.classList.toggle('busy', running || waiting);
  b.replaceChildren(...(running ? [el('span.spinner.xs'), ' Exporting…'] : waiting ? [el('span.spinner.xs'), ' Export starting…'] : ['Export']));
  b.dataset.tip = !S.tl.clips.length ? 'Add something to the story first' : running ? 'Making your video on this Mac…'
    : waiting ? 'Your export is next in line; it starts in a moment' : 'Make the final video file';
}
let exportPending = false;   // between the click and the server showing the job
async function doExport() {
  if (!S.tl.clips.length) return toast('Add something to the story first.');
  if ($('#btn-export').disabled) return;
  markFmtSeen();
  exportPending = true; renderExportBtn();
  try {
    await saveNow();
    await api.render(S.slug);
    toast(`Exporting ${fmtT(total())} of video as ${FORMATS[S.tl.aspect]?.name || S.tl.aspect}${S.tl.captions ? ' with captions' : ''}…`, { ms: 3500 });
    await refresh();
  } catch (e) { toast('Export didn’t start: ' + e.message, { kind: 'error' }); }
  finally { exportPending = false; renderExportBtn(); }
}

function renderSave() {
  const s = $('#save-status');
  const st = S.saveState;
  s.className = 'save-status ' + st;
  s.textContent = { saved: 'Saved', pending: 'Saving…', saving: 'Saving…', error: 'Not saved — retrying' }[st];
}

function clipName(c) { return c.label || S.server?.media?.[c.media]?.name || 'a clip'; }

/* ---------------- transport ---------------- */
function renderTime() {
  if (player.mode === 'source') return;
  $('#tp-time').textContent = fmtT(S.playhead);
  $('#tp-total').textContent = fmtT(total());
}
function renderPlay(playing) {
  $('#tp-play-icon').innerHTML = playing
    ? '<path d="M5 4h3.5v12H5zM11.5 4H15v12h-3.5z" fill="currentColor"/>'
    : '<path d="M6 4l11 6-11 6z" fill="currentColor"/>';
  document.body.classList.toggle('is-playing', !!playing);
}

/* ---------------- keyboard ---------------- */
const FRAME = 1 / 30;
function onKey(e) {
  if (e.key === 'Escape' && cancelGesture()) return;
  if (isTyping(e)) return;
  const mod = e.metaKey || e.ctrlKey;
  const k = e.key;
  if (mod && (k === 'z' || k === 'Z')) { e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); return; }
  if (mod && (k === 'y')) { e.preventDefault(); doRedo(); return; }
  if (mod && (k === '=' || k === '+')) { e.preventDefault(); setZoom(getZoom() * 1.4); return; }
  if (mod && k === '-') { e.preventDefault(); setZoom(getZoom() / 1.4); return; }
  if (mod && k === 's') { e.preventDefault(); saveNow(); toast('Saved'); return; }
  if (mod) return;
  switch (k) {
    case ' ': e.preventDefault(); player.toggle(); break;
    case 'k': case 'K': player.pause(); player.setRate(1); break;
    case 'l': case 'L':
      if (S.playing) player.setRate(Math.min(player.rate * 2, 4)); else { player.setRate(1); player.play(); }
      break;
    case 'j': case 'J':
      if (S.playing && player.rate > 1) player.setRate(player.rate / 2);
      else player.step(-2);
      break;
    case 'ArrowLeft': e.preventDefault(); player.pause(); player.step(e.shiftKey ? -1 : -FRAME); scrollToPlayhead(); break;
    case 'ArrowRight': e.preventDefault(); player.pause(); player.step(e.shiftKey ? 1 : FRAME); scrollToPlayhead(); break;
    case 'ArrowUp': e.preventDefault(); jumpEdge(-1); break;
    case 'ArrowDown': e.preventDefault(); jumpEdge(1); break;
    case 'Home': setPlayhead(0, { from: 'key' }); scrollToPlayhead(); break;
    case 'End': setPlayhead(total(), { from: 'key' }); scrollToPlayhead(); break;
    case 's': case 'S': splitAtPlayhead(); break;
    case 'Delete': case 'Backspace': e.preventDefault(); removeSelected(); break;
    case 'z': case 'Z': fit(); break;
    case 'Escape': if (player.mode === 'source') player.exitSource(); else select(null); break;
    case '?': openTab('clip'); select(null); break;
  }
}
/** Up/Down: jump to previous/next cut. */
function jumpEdge(dir) {
  const { starts, total: T } = layout(S.tl);
  const edges = [...starts, T];
  const t = S.playhead;
  const target = dir < 0 ? [...edges].reverse().find(x => x < t - 0.01) : edges.find(x => x > t + 0.01);
  if (target != null) { setPlayhead(target, { from: 'key' }); scrollToPlayhead(); }
}

/* ---------------- uploads & dropzone ---------------- */
const uploads = new Map();
function renderUploads() {
  const box = $('#uploads');
  if (!uploads.size) { box.hidden = true; return; }
  box.hidden = false;
  box.replaceChildren(el('div.up-title', { text: 'Adding footage' }), ...[...uploads.values()].map(u =>
    el('div.up', {}, el('div.up-name', { text: u.name }),
      el('div.up-bar', {}, el('div.up-fill', { style: { transform: `scaleX(${u.p})` } })),
      el('div.up-state', { text: u.state }))));
}
/** First footage in an empty studio: make a project for it instead of refusing the drop. */
async function ensureProject() {
  if (project()) return true;
  try {
    const name = 'Shoot ' + new Date().toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const r = await api.newProject(name, 'jefe');
    await refresh();
    setProject(r.slug);
    toast(`Started a project “${name}” for this footage. Rename it any time with + New.`, { kind: 'good', ms: 4500 });
    return true;
  } catch (e) { toast('Couldn’t start a project: ' + e.message, { kind: 'error' }); return false; }
}
const skippedToast = names => names.length && toast(`Skipped ${names.slice(0, 3).join(', ')}${names.length > 3 ? '…' : ''}: not a video or audio file.`, { kind: 'warn', ms: 5000 });

/** + Add footage: the native picker in the app, the browser's file input otherwise. */
async function addFootage() {
  if (!native) return $('#file-input').click();
  addPaths(await native.pickFootage());
}

/** The app: files (or folders) by path. The server reads them where they are and clones them
 *  into the library, so nothing is uploaded and the originals are never moved or changed. */
async function addPaths(paths) {
  if (!paths?.length) return;
  if (!await ensureProject()) return;
  try {
    const r = await api.ingestPaths(paths, S.slug, project()?.brand || 'jefe');
    const notMedia = r.skipped.filter(x => x.reason.startsWith('not a video')).map(x => x.name);
    const other = r.skipped.filter(x => !x.reason.startsWith('not a video'));
    if (!r.files.length && !other.length) return toast('Those don’t look like video or audio files.', { kind: 'warn' });
    skippedToast(notMedia);
    for (const x of other.slice(0, 3)) toast(`Skipped ${x.name}: ${x.reason}.`, { kind: 'warn', ms: 5000 });
    if (r.files.length) toast(`Adding ${r.files.length} file${r.files.length > 1 ? 's' : ''} to “${project()?.name || 'your project'}”`);
  } catch (e) { toast('Couldn’t add that: ' + e.message, { kind: 'error' }); }
  refresh();
}

async function uploadFiles(files) {
  const isMedia = f => /^(video|audio)\//.test(f.type) || /\.(mov|mp4|m4v|mkv|webm|avi|m4a|mp3|wav|aac|caf)$/i.test(f.name);
  const list = [...files].filter(isMedia);
  if (!list.length) return toast('Those don’t look like video or audio files.', { kind: 'warn' });
  skippedToast([...files].filter(f => !isMedia(f)).map(f => f.name));
  if (!await ensureProject()) return;
  const brand = project()?.brand || 'jefe';
  // two at a time: the disk and the loopback socket are both fast; more just thrashes
  const queue = list.slice();
  const runOne = async () => {
    const f = queue.shift();
    if (!f) return;
    const key = f.name + f.size + Math.random();
    uploads.set(key, { name: f.name, p: 0, state: 'Copying…' });
    renderUploads();
    try {
      await api.upload(f, S.slug, brand, p => { const u = uploads.get(key); u.p = p; u.state = `${Math.round(p * 100)}%`; renderUploadsSoon(); });
      const u = uploads.get(key); u.p = 1; u.state = 'Writing out words & previews…';
      renderUploads();
      setTimeout(() => { uploads.delete(key); renderUploads(); }, 2500);
    } catch (e) {
      const u = uploads.get(key); u.state = 'Failed: ' + e.message; renderUploads();
      setTimeout(() => { uploads.delete(key); renderUploads(); }, 8000);
    }
    refresh();
    return runOne();
  };
  toast(`Adding ${list.length} file${list.length > 1 ? 's' : ''} to “${project()?.name || 'a new project'}”`);
  await Promise.all([runOne(), runOne()]);
}
const renderUploadsSoon = debounce(renderUploads, 60);

function initDropzone() {
  const dz = $('#dropzone');
  let depth = 0;
  const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  const showDz = () => {
    $('#dz-sub').textContent = `into ${project() ? `“${project().name}”` : 'a new project'} — Studio writes out the words and makes previews on this Mac`;
    dz.hidden = false;
  };
  // In the app, files dragged in from Finder arrive as paths (the page never sees them as a
  // DOM drop); the UI's own drags, a moment onto the Story, stay ordinary DOM drags.
  native?.onDrag(d => {
    if (d.type === 'enter') showDz();
    else if (d.type === 'leave') dz.hidden = true;
    else if (d.type === 'drop') { dz.hidden = true; addPaths(d.paths); }
  });
  window.addEventListener('dragenter', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth++;
    showDz();
  });
  window.addEventListener('dragover', e => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  window.addEventListener('dragleave', e => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) dz.hidden = true; });
  window.addEventListener('drop', e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; dz.hidden = true;
    uploadFiles(e.dataTransfer.files);
  });
  $('#btn-add-footage').onclick = addFootage;
  $('#file-input').addEventListener('change', e => { uploadFiles(e.target.files); e.target.value = ''; });
}

/* ---------------- the app's menu bar ---------------- */
/** Edit ▸ Undo/Redo. In a text field they undo the typing (the field's own undo), anywhere
 *  else the story. ⌘Z itself reaches onKey first; the menu only gets it when onKey left it
 *  alone, i.e. while typing. */
function menuHistory(redo) {
  const t = document.activeElement;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA)$/.test(t.tagName)) && !/^(range|checkbox|button)$/.test(t.type)) {
    document.execCommand(redo ? 'redo' : 'undo');
  } else redo ? doRedo() : doUndo();
}
function initMenus() {
  native?.onMenu(id => {
    switch (id) {
      case 'new-project': newProjectDialog(); break;
      case 'add-footage': addFootage(); break;
      case 'share': shareDialog(); break;
      case 'import': importDialog(); break;
      case 'export': doExport(); break;
      case 'undo': menuHistory(false); break;
      case 'redo': menuHistory(true); break;
      case 'zoom-fit': fit(); break;
      case 'toggle-captions': $('#captions-toggle').click(); break;
    }
  });
}

/* Say plainly when something the studio needs isn't on this Mac. */
async function checkHealth() {
  const h = await api.getHealth().catch(() => null);
  if (!h) return;
  const box = $('#health'), notes = [];
  if (h.model && !h.model.exists) notes.push(['Words can’t be written out: the speech model isn’t on this Mac',
    `Studio needs Whisper’s model at ${h.model.path}. Until it’s there, footage gets previews and frames but no words, captions or moments.`]);
  if (h.tools && !h.tools.agent) notes.push(['Find moments needs a coding agent',
    h.agent?.error || `Studio can’t find the \`${h.agent?.command || 'agent'}\` command. Install your agent’s CLI (Claude Code, Codex, Goose…) so it works in Terminal, or set STUDIO_AGENT / STUDIO_AGENT_CMD (see the README), then reopen Studio.`]);
  if (h.tools && (!h.tools.ffmpeg || !h.tools['whisper-cli'])) notes.push(['Studio’s video tools are missing',
    'ffmpeg or whisper-cli isn’t on the PATH, so nothing can be prepared. Start Studio with ./studio ui or the app.']);
  if (!notes.length) { box.hidden = true; return; }
  box.hidden = false;
  box.textContent = '⚠ ' + notes.map(n => n[0]).join(' · ');
  box.dataset.tip = notes.map(n => n[1]).join('\n\n');
  toast(notes[0][0] + '. ' + notes[0][1], { kind: 'warn', ms: 12000 });
}

/* ---------------- footer ---------------- */
function renderCli() { $('#cli-line').textContent = cliFor(); }

/* ---------------- boot ---------------- */
async function boot() {
  initTooltips();
  initPlayer();
  initTimeline();
  initBin();
  initInspector();
  initDropzone();
  initMenus();

  $('#project-select').addEventListener('change', e => { player.pause(); setProject(e.target.value); });
  $('#btn-new-project').onclick = newProjectDialog;
  $('#btn-share').onclick = shareDialog;
  $('#btn-import').onclick = importDialog;
  on('openProject', slug => { player.pause(); setProject(slug); });
  $('#btn-undo').onclick = doUndo;
  $('#btn-redo').onclick = doRedo;
  $('#btn-format').onclick = () => { $('#format-menu').hidden ? openFormatMenu() : ($('#format-menu').hidden = true); };
  $('#captions-toggle').addEventListener('change', e => {
    markFmtSeen();
    commit(setCaptions(S.tl, e.target.checked), e.target.checked ? 'Captions on' : 'Captions off');
  });
  $('#btn-export').onclick = doExport;
  $('#tp-play').onclick = () => player.toggle();
  $('#tp-back').onclick = () => player.step(-1);
  $('#tp-fwd').onclick = () => player.step(1);
  $('#tp-start').onclick = () => { setPlayhead(0, { from: 'key' }); scrollToPlayhead(); };
  $('#tp-end').onclick = () => { setPlayhead(total(), { from: 'key' }); scrollToPlayhead(); };
  $('#tp-mute').onclick = () => player.toggleMute();
  $('#btn-delete').onclick = removeSelected;
  $('#btn-copy-cli').onclick = () => copyText($('#cli-line').textContent);
  $('#stage').addEventListener('click', () => player.toggle());
  $('#stage-empty').replaceChildren(
    el('div.empty-title', { text: 'Your video shows up here' }),
    el('div.empty-sub', { text: 'Add moments to the Story below and press Space to watch it.' }));
  document.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', e => { if (dirty()) { saveNow(); e.preventDefault(); } });

  on('server', () => { renderProjects(); renderSteps(); renderExportBtn(); });
  on('project', () => { renderProjects(); renderSteps(); renderFormat(); renderCli(); renderTime(); });
  on('timeline', d => {
    if (d?.preview) { renderTime(); return; }
    renderFormat(); renderSteps(); renderExportBtn(); renderTime(); renderCli();
    if (d?.remote) {
      const who = isAgent(d.actor) ? 'The agent' : d.actor === 'operator' ? 'Another window' : 'The terminal';
      if (d.conflict && d.merged) toast(`${who} edited the story while you were editing${d.prev && diffStory(d.prev, S.tl, clipName).summary ? ` (${diffStory(d.prev, S.tl, clipName).summary})` : ''}. Both changes are kept.`, { kind: 'agent', ms: 5000, actions: [{ label: 'Undo', run: doUndo }] });
      else if (d.conflict) toast(`${who} changed the same clip you were editing. Showing their version. Undo brings back yours.`, { kind: 'agent', ms: 7000, actions: [{ label: 'Bring mine back', run: doUndo }] });
      else {
        const what = d.prev ? diffStory(d.prev, S.tl, clipName).summary : '';
        toast(what ? `${who} ${what}` : `${who} edited the story`, { kind: 'agent', ms: what.startsWith('removed') ? 7000 : 5000, actions: [{ label: 'Undo', run: doUndo }] });
      }
    }
  });
  on('formatTag', renderFormat);
  on('history', renderHistory);
  on('save', renderSave);
  on('select', renderCli);
  on('playhead', renderTime);
  on('playstate', renderPlay);
  on('undo', doUndo);
  on('focusMediaReq', id => import('./store.js').then(s => s.focusMedia(id)));
  on('exported', ev => {
    const x = { ...ev.data, ts: ev.ts };
    showExport(x.file);
    // exported is not ready: only a check of these exact bytes makes it ready
    native?.notify('Your video is exported', `“${project()?.name || 'Your project'}” is exported. Check it before you post it.`);
    toast('Your video is exported. Not checked yet.', { kind: 'good', ms: 10000, actions: [
      { label: 'Check', run: () => api.checkExport(S.slug).catch(e => toast(e.message, { kind: 'error' })) },
      { label: 'Watch', run: () => playExport(x) },
      { label: 'Copy post', run: () => emit('postsForStory') },
      { label: 'Show in Finder', run: () => api.reveal(x.file) }] });
  });
  on('checked', ev => {
    const d = ev.data || {};
    const exc = (d.excepted || []).length;
    if (d.ok) toast(`Ready: it clears the bar${exc ? ` (${exc} exception${exc > 1 ? 's' : ''}, listed under Your videos)` : ''}.`, { kind: 'good', ms: 7000 });
    else toast(`Not ready: ${(d.failed || []).join(', ')} to fix.`, { kind: 'warn', ms: 7000 });
  });
  on('posted', () => { if (S.slug) localStorage.setItem('studio.posted.' + S.slug, '1'); renderSteps(); });
  renderHistory(); renderSave(); renderPlay(false);

  await initLive();
  renderFormat();
  checkHealth();
  // first useful view: select the first media of the project so moments are visible
  if (!S.media) { const pm = projectMedia(); const m = pm.find(x => x.moments?.length) || pm[0]; if (m) import('./store.js').then(s => s.focusMedia(m.id)); }
  // prefetch transcripts so captions & word-snapping are instant
  for (const m of projectMedia()) if (m.steps?.transcribe) api.getTranscript(m.id, m.steps.transcribe.sha256 || 't');
  on('project', () => { if (!S.media) { const pm = projectMedia(); const m = pm.find(x => x.moments?.length) || pm[0]; if (m) import('./store.js').then(s => s.focusMedia(m.id)); } });
  on('project', () => { for (const m of projectMedia()) if (m.steps?.transcribe) api.getTranscript(m.id, m.steps.transcribe.sha256 || 't'); });
  window.__studio = { S, player };   // for debugging from the console
}

boot().catch(e => { console.error(e); toast('Studio failed to start: ' + e.message, { kind: 'error', ms: 10000 }); });
