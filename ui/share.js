// Share project… and Import…: the UI over `studio bundle` and `studio import`.
// A bundle is one folder with the original footage and everything done to it (words,
// moments, drafts, stories, finished videos); the teammate imports it into their studio.

import { S } from './store.js';
import * as api from './api.js';
import { el, toast, modal, copyText } from './util.js';
import { native } from './native.js';

const q = s => /^[\w@%+=:,./-]+$/.test(s) ? s : `"${s.replace(/(["\\$`])/g, '\\$1')}"`;

function cliBox(cmd) {
  const code = el('code.cli-line', { text: cmd() });
  return {
    node: el('div.field', {}, el('span', { text: 'Or in Terminal' }),
      el('div.share-cli', {}, code, el('button.btn.ghost.xs', { text: 'Copy', onclick: () => { copyText(cmd()); toast('Copied'); } }))),
    update: () => { code.textContent = cmd(); },
  };
}

function folderField(label, value, placeholder, title, onChange) {
  const input = el('input.text', { value, placeholder, spellcheck: false });
  input.addEventListener('keydown', e => e.stopPropagation());
  input.addEventListener('input', onChange);
  const pick = native?.pickFolder && el('button.btn', { text: 'Choose…', onclick: async () => {
    const p = await native.pickFolder(title);
    if (p) { input.value = p; onChange(); }
  } });
  return { input, node: el('label.field', {}, el('span', { text: label }), el('div.share-row', {}, input, pick || '')) };
}

/** Share project…: pick projects and a folder; the server writes the bundle in the background. */
export function shareDialog() {
  const ps = Object.values(S.server?.projects || {}).filter(p =>
    p.timeline?.clips?.length || Object.values(S.server?.media || {}).some(m => m.project === p.slug));
  if (!ps.length) return toast('Nothing to share yet: add some footage first.');
  modal((card, close) => {
    const picked = new Set([S.slug].filter(s => ps.some(p => p.slug === s)));
    if (!picked.size) picked.add(ps[0].slug);
    const nameFor = () => (ps.find(p => picked.has(p.slug))?.name || 'Studio').replace(/[/:\\]+/g, '-');
    let auto = true;
    const where = folderField('Save it as (a new folder)', `~/Desktop/${nameFor()}`, '/Users/you/Desktop/BMW job',
      'Where to save the bundle', () => { auto = false; cli.update(); });
    if (native?.pickFolder) where.node.querySelector('button').onclick = async () => {
      const dir = await native.pickFolder('Choose where to save the bundle');
      if (dir) { where.input.value = `${dir}/${nameFor()}`; auto = false; cli.update(); }
    };
    const zip = el('input', { type: 'checkbox', checked: true });
    const cmd = () => `studio bundle ${q(where.input.value.trim() || 'OUT_DIR')} ${[...picked].map(s => `-p ${q(s)}`).join(' ')}${zip.checked ? ' --zip' : ''}`;
    const cli = cliBox(cmd);
    zip.addEventListener('change', cli.update);
    const list = el('div.share-list', {}, ps.map(p => {
      const n = Object.values(S.server?.media || {}).filter(m => m.project === p.slug).length;
      const box = el('input', { type: 'checkbox', checked: picked.has(p.slug), onchange: e => {
        e.target.checked ? picked.add(p.slug) : picked.delete(p.slug);
        if (auto) where.input.value = where.input.value.replace(/[^/]*$/, nameFor());
        cli.update();
      } });
      return el('label.share-item', {}, box, el('span', { text: p.slug === 'inbox' ? 'Inbox (unsorted)' : p.name }),
        el('span.hint', { text: `${n} file${n === 1 ? '' : 's'} · ${p.timeline?.clips?.length || 0} clip${p.timeline?.clips?.length === 1 ? '' : 's'}` }));
    }));
    const go = async () => {
      const out = where.input.value.trim();
      if (!picked.size) return toast('Choose at least one project.');
      if (!out) return where.input.focus();
      try {
        await api.share([...picked], out, zip.checked);
        close();
        toast(`Packing ${picked.size === 1 ? 'the project' : `${picked.size} projects`} into ${out}… you can keep working.`, { ms: 4500 });
      } catch (e) { toast(e.message, { kind: 'error', ms: 6000 }); }
    };
    card.append(el('div.modal-head', {}, el('h2', { text: 'Share project' })),
      el('p.hint', { text: 'Makes one folder with the original footage and everything done to it: words, moments, post drafts, the story and finished videos. Send it (zipped) through Google Drive; your teammate opens it with Import.' }),
      list, where.node,
      el('label.share-item', {}, zip, el('span', { text: 'Also make a .zip to upload' }), el('span.hint', { text: 'not compressed; video doesn’t shrink' })),
      cli.node,
      el('div.modal-actions', {}, el('button.btn', { text: 'Cancel', onclick: close }), el('button.btn.primary', { text: 'Share', onclick: go })));
  });
}

/** Import…: a bundle folder from a teammate. */
export function importDialog() {
  modal((card, close) => {
    const where = folderField('The shared folder (unzip it first)', '', '/Users/you/Downloads/BMW 200 Job',
      'Choose the shared folder to import', () => cli.update());
    const cli = cliBox(() => `studio import ${q(where.input.value.trim() || 'BUNDLE_DIR')}`);
    const go = async () => {
      const p = where.input.value.trim();
      if (!p) return where.input.focus();
      try {
        await api.importBundle(p);
        close();
        toast('Checking the files, then bringing them in… you can keep working.', { ms: 4500 });
      } catch (e) { toast(e.message, { kind: 'error', ms: 6000 }); }
    };
    where.input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
    card.append(el('div.modal-head', {}, el('h2', { text: 'Import shared footage' })),
      el('p.hint', { text: 'Brings in a folder a teammate shared from their studio: the footage, its words, moments, drafts and stories. Every file is checked first; the folder itself is never changed.' }),
      where.node, cli.node,
      el('div.modal-actions', {}, el('button.btn', { text: 'Cancel', onclick: close }), el('button.btn.primary', { text: 'Import', onclick: go })));
    if (native?.pickFolder) setTimeout(() => where.node.querySelector('button')?.click(), 0);
  });
}
