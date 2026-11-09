// Small shared helpers: DOM, time, scheduling, toasts, tooltips.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** el('div.cls#id', {attrs}, ...children) */
export function el(spec, attrs = {}, ...kids) {
  const m = spec.match(/^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i);
  const node = document.createElement((m && m[1]) || 'div');
  if (m && m[2]) for (const p of m[2].match(/[.#][\w-]+/g)) {
    if (p[0] === '.') node.classList.add(p.slice(1)); else node.id = p.slice(1);
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') node.className += ' ' + v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) node.append(k.nodeType ? k : String(k));
  return node;
}

export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const round3 = v => Math.round(v * 1000) / 1000;

/** 0:08.1 style, matching the CLI's fmt_t. */
export function fmtT(t, digits = 1) {
  t = Math.max(0, +t || 0);
  const m = Math.floor(t / 60), s = t - m * 60;
  const ss = s.toFixed(digits).padStart(digits ? 3 + digits : 2, '0');
  return `${m}:${ss}`;
}
export function fmtDur(t) {
  t = Math.max(0, +t || 0);
  if (t < 60) return `${t.toFixed(t < 10 ? 1 : 0)}s`;
  const m = Math.floor(t / 60), s = Math.round(t - m * 60);
  return `${m}m ${String(s).padStart(2, '0')}s`;
}
export function timeAgo(ts) {
  const d = (Date.now() - new Date(ts).getTime()) / 1000;
  if (!isFinite(d)) return '';
  if (d < 10) return 'just now';
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return new Date(ts).toLocaleDateString();
}

export function debounce(fn, ms) {
  let t;
  const f = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  f.flush = (...a) => { clearTimeout(t); fn(...a); };
  f.cancel = () => clearTimeout(t);
  return f;
}
export function throttle(fn, ms) {
  let last = 0, t = null, args;
  return (...a) => {
    args = a;
    const now = performance.now();
    if (now - last >= ms) { last = now; fn(...args); }
    else if (!t) t = setTimeout(() => { t = null; last = performance.now(); fn(...args); }, ms - (now - last));
  };
}
/** Coalesce many calls per frame into one, on the next animation frame. */
export function rafBatch(fn) {
  let queued = false;
  return () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(); });
  };
}

export function isTyping(e) {
  const t = e.target;
  return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) &&
    !(t.type === 'range' || t.type === 'checkbox');
}

export const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = el('textarea', { style: { position: 'fixed', opacity: 0 } });
    ta.value = text; document.body.append(ta); ta.select();
    document.execCommand('copy'); ta.remove();
  }
  toast('Copied');
}

/* ---- toasts ---- */
export function toast(msg, opts = {}) {
  const box = $('#toasts');
  const t = el('div.toast' + (opts.kind ? '.' + opts.kind : ''), {}, el('span', { text: msg }));
  for (const a of opts.actions || []) {
    t.append(el('button.btn.xs', { text: a.label, onclick: () => { a.run(); t.remove(); } }));
  }
  box.append(t);
  requestAnimationFrame(() => t.classList.add('in'));
  setTimeout(() => { t.classList.remove('in'); setTimeout(() => t.remove(), 300); }, opts.ms || 2600);
  return t;
}

/* ---- tooltips: one floating element, driven by [data-tip] ---- */
export function initTooltips() {
  const tip = $('#tooltip');
  let timer = null, cur = null;
  const hide = () => { clearTimeout(timer); tip.hidden = true; cur = null; };
  document.addEventListener('pointerover', e => {
    const t = e.target.closest?.('[data-tip]');
    if (t === cur) return;
    hide();
    if (!t || e.buttons) return;
    cur = t;
    timer = setTimeout(() => {
      if (!cur || !document.contains(cur)) return;
      tip.textContent = cur.dataset.tip;
      tip.hidden = false;
      const r = cur.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
      let x = r.left + r.width / 2 - w / 2, y = r.bottom + 8;
      if (y + h > innerHeight - 6) y = r.top - h - 8;
      x = clamp(x, 6, innerWidth - w - 6);
      tip.style.transform = `translate(${x}px, ${y}px)`;
    }, 450);
  });
  document.addEventListener('pointerdown', hide, true);
  document.addEventListener('keydown', hide, true);
  window.addEventListener('blur', hide);
}

/* ---- modal ---- */
export function modal(build) {
  const m = $('#modal'), card = $('#modal-card');
  card.replaceChildren();
  const close = () => { m.hidden = true; card.replaceChildren(); document.removeEventListener('keydown', esc, true); };
  const esc = e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  document.addEventListener('keydown', esc, true);
  m.onclick = e => { if (e.target === m) close(); };
  build(card, close);
  m.hidden = false;
  const f = card.querySelector('[autofocus]') || card.querySelector('input,textarea,button');
  f?.focus();
  return close;
}

/** Binary search: index of last item with key(item) <= t, or -1. */
export function bsearch(arr, t, key) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(arr[mid]) <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}
