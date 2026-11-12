// Preview player. Plays the whole story seamlessly across clips with two <video> elements:
// the visible one plays, the hidden one is pre-loaded and pre-seeked to the next clip, and at
// the cut we swap them. Scrubbing coalesces seeks (one in flight at a time, latest wins).
// Also a "source" mode to preview raw footage / a moment before it's in the story.

import { S, on, emit, setPlayhead, mediaOf, total } from './store.js';
import { clipAt, layout, dur, paceOf } from './ops.js';
import { transcriptNow } from './api.js';
import { $, el, fmtT, clamp, bsearch } from './util.js';
import { FORMATS, clipFit } from './formats.js';
import { captionChunks } from './captions.js';

const EPS = 1 / 60;
const PREROLL = 0.5;    // seconds of warm-up for the next clip (audio clock start-up eats ~80ms)
const PREROLL_LEAD = 0.04;

export function mediaSrc(m) {
  if (!m) return '';
  const p = m.steps?.proxy;
  // ?v=<receipt>: the server caches versioned media URLs forever; a re-made proxy gets a new URL
  if (p && !p.skipped) return `/files/media/${m.id}/${p.file || 'proxy.mp4'}` + (p.sha256 ? `?v=${p.sha256.slice(0, 16)}` : '');
  if (m.source) return '/files/' + m.source.split('/').map(encodeURIComponent).join('/');
  const ext = (m.name?.match(/\.[a-z0-9]+$/i)?.[0] || '.mp4').toLowerCase();
  return `/files/media/${m.id}/source${ext}`;
}
export const transcriptStamp = m => m?.steps?.transcribe?.sha256 || (m?.steps?.transcribe ? 't' : '');

class Player {
  constructor() {
    this.v = [$('#vid-a'), $('#vid-b')];
    this.act = 0;
    this.stage = $('#stage');
    this.wrap = $('#stage-wrap');
    this.cap = $('#cap-overlay');
    this.bg = $('#stage-bg');       // 'fit' clips: a blurred copy of the frame behind it
    this.bgCtx = this.bg?.getContext('2d');
    this.fitOn = false;
    this.rate = 1;
    this.muted = localStorage.getItem('studio.muted') === '1';
    this.mode = 'story';          // story | source
    this.source = null;           // {mid, a, b}
    this.cur = null;              // index of clip on the active element
    this.raf = 0;
    this.capKey = '';
    // how far ahead of "in step" the pre-roll seeks (learned per engine: see the cut below)
    this.lead = clamp(+localStorage.getItem('studio.prerollLead') || PREROLL_LEAD, 0, 0.4);
    for (const v of this.v) {
      v.muted = false;
      v.preload = 'auto';
      v._pending = null;
      v.addEventListener('seeked', () => { this._seekDone(v); if (v === this.a) this.paintBg(); });
      v.addEventListener('loadeddata', () => { if (v === this.a) this.paintBg(); });
      v.addEventListener('waiting', () => this._waiting(true));
      v.addEventListener('playing', () => this._waiting(false));
      v.addEventListener('canplay', () => this._waiting(false));
      v.addEventListener('error', () => { if (v.src) this._waiting(true, 'This footage is still being prepared…'); });
    }
    new ResizeObserver(() => this.fit()).observe(this.wrap);
    on('timeline', d => this.onTimeline(d));
    on('playhead', d => { if (d?.from !== 'player') this.onSeek(); this.drawCaption(); });
    on('server', () => this.drawCaption());
    window.addEventListener('studio:transcript', () => this.drawCaption());
    on('project', () => { this.exitSource(false); this.pause(); this.show(); });
    this.fit();
    this.applyMute();
  }

  setLead(x) {
    this.lead = clamp(x, 0, 0.4);
    localStorage.setItem('studio.prerollLead', this.lead.toFixed(3));
  }

  get a() { return this.v[this.act]; }
  get b() { return this.v[1 - this.act]; }

  /* ---------- layout ---------- */
  fit() {
    const f = FORMATS[S.tl.aspect] || FORMATS['9:16'];
    const ar = this.mode === 'source' && this.source?.ar ? this.source.ar : f.w / f.h;
    const W = this.wrap.clientWidth - 40, H = this.wrap.clientHeight - 40;
    let w = Math.min(W, H * ar), h = w / ar;
    w = Math.max(40, Math.floor(w)); h = Math.max(40, Math.floor(h));
    this.stage.style.width = w + 'px';
    this.stage.style.height = h + 'px';
    this.stage.style.setProperty('--cap-size', (Math.min(w, h) * 0.08).toFixed(1) + 'px');
    this.stage.style.setProperty('--cap-margin', (w / 13).toFixed(1) + 'px');
    this.stage.classList.toggle('source-mode', this.mode === 'source');
  }

  applyMute() {
    // Inactive element: volume 0 rather than muted, so its audio output is already running at
    // the cut (unmuting a muted element re-initialises audio and stalls its clock ~80ms).
    for (const v of this.v) { v.muted = this.muted; v.volume = v === this.a ? 1 : 0.0001; }  // not 0: Chrome suspends a silent sink
    $('#tp-mute-icon').innerHTML = this.muted
      ? '<path d="M3 8h3l4-3v10l-4-3H3zM13 7l5 6M18 7l-5 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>'
      : '<path d="M3 8h3l4-3v10l-4-3H3z" fill="currentColor"/><path d="M13 7a4 4 0 010 6M15 5a7 7 0 010 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>';
  }
  toggleMute() { this.muted = !this.muted; localStorage.setItem('studio.muted', this.muted ? '1' : '0'); this.applyMute(); }

  _waiting(on, msg) {
    const w = $('#stage-wait');
    if (on && msg) w.querySelector('span:last-child').textContent = msg;
    else w.querySelector('span:last-child').textContent = 'Getting the preview ready…';
    clearTimeout(this._wt);
    if (on) this._wt = setTimeout(() => { w.hidden = false; }, 250);
    else w.hidden = true;
  }

  /* ---------- fill / fit (same rule as the export: formats.js clipFit) ---------- */
  setFitFor(clip) {
    const on = !!clip && this.mode !== 'source' && clipFit(clip, mediaOf(clip.media), S.tl.aspect) === 'fit';
    this.fitOn = on;
    this.stage.classList.toggle('fit-mode', on);
    if (on) this.paintBg();
  }
  /** Paint the active frame, cover-cropped, into the small background canvas (CSS blurs it). */
  paintBg() {
    if (!this.fitOn || !this.bgCtx) return;
    const v = this.a, vw = v.videoWidth, vh = v.videoHeight;
    if (!vw || !vh || v.readyState < 2) return;
    const W = 48, H = Math.max(8, Math.round(48 * (this.stage.clientHeight || 16) / (this.stage.clientWidth || 9)));
    if (this.bg.width !== W || this.bg.height !== H) { this.bg.width = W; this.bg.height = H; }
    const s = Math.max(W / vw, H / vh), sw = W / s, sh = H / s;
    try { this.bgCtx.drawImage(v, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, W, H); } catch { /* not decodable yet */ }
  }

  /* ---------- low-level video control ---------- */
  load(v, mid) {
    const src = mediaSrc(mediaOf(mid));
    if (v._mid !== mid || v._src !== src) {
      v._mid = mid; v._src = src; v._pending = null; v._seekingTo = null;
      v.src = src;
    }
  }
  /** Coalesced seek: at most one seek in flight per element; the latest target wins.
   *  exact=false may use fastSeek (WebKit has it, Chrome doesn't): it lands on the nearest
   *  keyframe (the proxies have one every 15 frames), fine while scrubbing, never for a frame
   *  you're judging. */
  seek(v, t, exact = true) {
    if (v.readyState < 1) {      // no metadata yet: seek as soon as it arrives
      v._pending = t;
      v.addEventListener('loadedmetadata', () => { if (v._pending != null) { const p = v._pending; v._pending = null; this.seek(v, p, exact); } }, { once: true });
      return;
    }
    if (v._seekingTo != null) { v._pending = t; return; }
    if (Math.abs(v.currentTime - t) < 0.002) return;
    v._seekingTo = t;
    if (!exact && typeof v.fastSeek === 'function') v.fastSeek(t); else v.currentTime = t;
  }
  _seekDone(v) {
    v._seekingTo = null;
    if (v._pending != null) { const p = v._pending; v._pending = null; this.seek(v, p); }
  }
  swap() {
    this.act = 1 - this.act;
    this.a.classList.add('active');
    this.b.classList.remove('active');
    this.applyMute();
  }

  /* ---------- story playback ---------- */
  /** Show the frame at the current playhead (paused). */
  show(fast = false) {
    if (this.mode === 'source') return;
    const at = clipAt(S.tl, S.playhead);
    $('#stage-empty').hidden = !!at;
    this.stage.classList.toggle('empty', !at);
    if (!at) { for (const v of this.v) v.classList.remove('visible'); this.cur = null; this.setFitFor(null); return; }
    for (const v of this.v) v.classList.add('visible');
    this.setFitFor(at.clip);
    let v = this.a;
    if (v._mid !== at.clip.media && this.b._mid === at.clip.media) { this.swap(); v = this.a; }
    this.load(v, at.clip.media);
    this.seek(v, at.src, !fast);
    this.cur = at.i;
    this.preroll = false;
    this.b.pause();
    this.preloadNext();
  }
  preloadNext() {
    const next = S.tl.clips[this.cur + 1];
    if (!next) return;
    const c = S.tl.clips[this.cur];
    if (c && next.media === c.media && Math.abs(next.in - c.out) < 0.05) return; // continuous: no swap needed
    this.load(this.b, next.media);
    this.seek(this.b, next.in);
  }

  get playing() { return S.playing; }
  toggle() { this.playing ? this.pause() : this.play(); }

  play() {
    if (this.mode === 'source') return this.playSource();
    if (!S.tl.clips.length) return;
    if (S.playhead >= total() - 0.05) setPlayhead(0, { from: 'player' });
    this.show();
    S.playing = true;
    this.a.playbackRate = this.rate;
    const p = this.a.play();
    p?.catch?.(() => {});
    emit('playstate', true);
    this.loop();
  }
  pause() {
    S.playing = false;
    cancelAnimationFrame(this.raf);
    for (const v of this.v) v.pause();
    emit('playstate', false);
  }
  setRate(r) {
    this.rate = r;
    for (const v of this.v) v.playbackRate = r;
    const tag = $('#tp-rate');
    tag.hidden = r === 1; tag.textContent = r + '×';
  }

  loop() {
    cancelAnimationFrame(this.raf);
    const tick = () => {
      if (!S.playing) return;
      if (this.mode === 'source') { this.tickSource(); this.raf = requestAnimationFrame(tick); return; }
      const clips = S.tl.clips;
      const c = clips[this.cur];
      if (!c) { this.pause(); return; }
      const v = this.a;
      const t = v.currentTime;
      const nx = clips[this.cur + 1];
      // pre-roll: start the hidden element a moment early so its decoder and audio are already
      // running at the cut (a cold play() stalls ~80ms, which reads as a hiccup)
      if (nx && !this.preroll && c.out - t < PREROLL && c.out - t > 0.02 &&
          !(nx.media === c.media && Math.abs(nx.in - c.out) < 0.05)) {
        const nv = this.b;
        if (nv._mid === nx.media && nv.readyState >= 2) {
          nv.currentTime = Math.max(0, nx.in - (c.out - t) + this.lead);
          nv._seekingTo = null; nv._pending = null;
          nv.playbackRate = this.rate;
          nv.play()?.catch?.(() => {});
          this.preroll = true;
        }
      }
      if (t >= c.out - EPS || v.ended) {
        const next = clips[this.cur + 1];
        if (!next) {
          this.pause();
          setPlayhead(total(), { from: 'player' });
          return;
        }
        const continuous = next.media === c.media && Math.abs(next.in - c.out) < 0.05;
        this.cur++;
        const prerolled = this.preroll;
        this.preroll = false;
        if (!continuous) {
          const nv = this.b;
          if (nv._mid !== next.media) this.load(nv, next.media);
          // learn how far ahead the pre-roll must aim: a seeked element gets going ~0.04 s late
          // in Chrome but ~0.2 s late in WebKit, and arriving short means a re-seek (a freeze)
          if (prerolled && !nv.paused && nv._mid === next.media) {
            const short = next.in - nv.currentTime;
            if (Math.abs(short) < 0.5) this.setLead(this.lead + 0.8 * short);
          }
          if (Math.abs(nv.currentTime - next.in) > 0.12) this.seek(nv, next.in);
          nv.playbackRate = this.rate;
          if (nv.paused) nv.play()?.catch?.(() => {});
          this.swap();
          v.pause();
        }
        this.setFitFor(next);
        this.preloadNext();
        setPlayhead(layout(S.tl).starts[this.cur], { from: 'player' });
      } else if (v.readyState >= 2 && v._seekingTo == null) {
        const st = layout(S.tl).starts[this.cur] + clamp(t - c.in, 0, dur(c));
        setPlayhead(st, { from: 'player' });
      }
      if (this.fitOn) this.paintBg();
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }

  onSeek() {
    if (this.mode === 'source') this.exitSource(false);
    if (S.playing) {
      const at = clipAt(S.tl, S.playhead);
      if (!at) return this.pause();
      if (at.i !== this.cur || Math.abs(this.a.currentTime - at.src) > 0.25) {
        this.show();
        this.a.play()?.catch?.(() => {});
      }
    } else {
      this.show(this._scrubbing);
    }
  }
  onTimeline(d) {
    this.fit();
    if (this.mode === 'source') return;
    if (d?.preview) return;         // trims call peek() themselves
    const wasPlaying = S.playing;
    if (wasPlaying) { this.show(); this.a.play()?.catch?.(() => {}); } else this.show();
    this.drawCaption();
  }
  set scrubbing(on) { this._scrubbing = on; }

  /** Show one frame of a source while trimming (doesn't move the playhead). Exact: the
   *  frame at the edge you're dragging, not the keyframe near it. */
  peek(mid, t) {
    if (S.playing) this.pause();
    const v = this.a;
    v.classList.add('visible');
    $('#stage-empty').hidden = true;
    this.load(v, mid);
    this.seek(v, t);
    this.cur = null;
  }
  unpeek() { this.show(); }

  step(delta) {
    if (this.mode === 'source') {
      const v = this.a;
      this.seek(v, clamp(v.currentTime + delta, 0, v.duration || 1e9));
      return;
    }
    setPlayhead(S.playhead + delta, { from: 'step' });
  }

  /* ---------- source preview mode ---------- */
  previewSource(mid, a = 0, b = null, autoplay = true) {
    const m = mediaOf(mid);
    if (!m) return;
    this.pause();
    this.mode = 'source';
    this.setFitFor(null);
    this.source = { mid, a, b: b ?? m.duration, ar: m.width && m.height ? m.width / m.height : 16 / 9 };
    $('#stage-empty').hidden = true;
    this.stage.classList.remove('empty');
    for (const v of this.v) v.classList.add('visible');
    const tag = $('#stage-format-tag');
    tag.replaceChildren(
      el('span', { text: `Previewing “${m.name}”${b != null ? ` · ${fmtT(a)}–${fmtT(b)}` : ''} — not in your story yet` }),
      el('button.btn.xs', { text: 'Back to story', onclick: () => this.exitSource() }));
    tag.classList.add('source');
    this.fit();
    this.load(this.a, mid);
    this.seek(this.a, a);
    this.cap.textContent = '';
    if (autoplay) this.playSource();
  }
  playSource() {
    const v = this.a, s = this.source;
    if (!s) return;
    if (v.currentTime >= s.b - 0.05 || v.currentTime < s.a - 0.5) this.seek(v, s.a);
    S.playing = true;
    v.playbackRate = this.rate;
    v.play()?.catch?.(() => {});
    emit('playstate', true);
    this.loop();
  }
  tickSource() {
    const v = this.a, s = this.source;
    if (v.currentTime >= s.b - EPS || v.ended) { this.pause(); }
    $('#tp-time').textContent = fmtT(v.currentTime);
    $('#tp-total').textContent = fmtT(s.b);
  }
  exitSource(reshow = true) {
    if (this.mode !== 'source') return;
    this.pause();
    this.mode = 'story';
    this.source = null;
    const tag = $('#stage-format-tag');
    tag.classList.remove('source');
    emit('formatTag');
    this.fit();
    if (reshow) { this.show(); emit('playhead', { from: 'player' }); }
  }

  /* ---------- captions overlay (captions.js: the same chunks as the export) ---------- */
  drawCaption() {
    if (this.mode === 'source' || !S.tl.captions) { if (this.capKey) { this.cap.textContent = ''; this.capKey = ''; } return; }
    const at = clipAt(S.tl, S.playhead);
    let text = '';
    if (at) {
      const chunks = this.chunksFor(at.clip);
      if (chunks) {
        const k = bsearch(chunks, at.local, c => c.a);
        if (k >= 0 && at.local < chunks[k].b) text = chunks[k].text;
      }
    }
    if (text !== this.capKey) { this.cap.textContent = text; this.capKey = text; }
  }
  chunksFor(clip) {
    const m = mediaOf(clip.media);
    const t = transcriptNow(clip.media, transcriptStamp(m));
    if (!t) return null;
    this._chunks ||= new Map();
    const key = `${clip.media}|${transcriptStamp(m)}|${clip.in}|${clip.out}|${paceOf(clip)}|${t.words.length}|${t.segments.length}`;
    let ch = this._chunks.get(key);
    if (!ch) {
      ch = captionChunks(t.words, t.segments, clip.in, clip.out, paceOf(clip));
      if (this._chunks.size > 200) this._chunks.clear();
      this._chunks.set(key, ch);
    }
    return ch;
  }
}

export let player;
export function initPlayer() { player = new Player(); return player; }
