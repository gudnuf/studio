/* Jefe UI mocks — deterministic, frame-addressable.
 *
 * Every frame is a pure function of t: renderAt(t) rebuilds the screen's DOM, then runs the
 * layout fix-ups (scroll anchoring) that need measured sizes. No CSS transitions or timers,
 * so headless Chrome can step it frame by frame (render.py).
 *
 * Built from damsac/sitewalk @ origin/main (d8ce883). Source of every number is noted:
 *   Theme.swift (tokens + type ramp), Controls.swift (buttons, RecBanner, Waveform),
 *   Primitives.swift (FieldTag, SectionLabel, MetaStrip, Caret, GapChip, EmptyPanel),
 *   Rows.swift (CapturedRow, SectionHead), BoardView.swift, WalkView.swift, NotesView.swift,
 *   ReviewView.swift, Document.swift (Letterhead, DocRowView, DocSectionView, TotalRow, RevNote),
 *   MurmurEngineFormatting.swift (tags, note/send copy), GapWarning.swift, Fixtures.swift.
 */
const JefeMock = (() => {
  // ---------------------------------------------------------------- tokens (Theme.swift)
  const C = {
    paper: '#FAFAF7', paperDeep: '#F1F0EA', sheet: '#FFFFFE', ink: '#141412',
    ink60: '#5E5C54', ink35: '#A7A49A', ink45: '#6E6B62',
    hairline: 'rgba(20,20,18,0.16)', hairlineSoft: 'rgba(20,20,18,0.09)',
    orange: '#FFBB26', orangeDeep: '#9A6A00', amberInk: '#6B4900', orangeTint: '#FAF1D9', onOrange: '#141412',
    redTag: '#A63A2E', redTint: '#F7E8E5', yellowTag: '#9A7213', yellowTint: '#F6EFD9',
    greenTag: '#3E6B35', greenTint: '#E9F0E4', ink32: 'rgba(20,20,18,0.32)',
  };
  const PAD = 20;             // Theme.S.screenPad
  const W = 393, H = 852;     // iPhone 15/16 Pro logical points
  const SAFE_TOP = 54, SAFE_BOTTOM = 34;

  // Theme.F.scaled: the continuous type-ramp taper (1.30x at <=12pt to 1.08x at >=20pt, 11pt floor).
  function sc(size) {
    if (size <= 12) return Math.max(11, size * 1.30);
    if (size >= 20) return size * 1.08;
    const t = (size - 12) / 8;
    return size * (1.30 + t * (1.08 - 1.30));
  }
  const WT = { regular: 400, medium: 500, semibold: 600, bold: 700, extraBold: 800 };
  const F = {
    ui: (s, w = 'semibold') => `font-family:Barlow;font-weight:${WT[w]};font-size:${sc(s).toFixed(2)}px;`,
    cond: (s, w = 'semibold') => `font-family:BarlowSC;font-weight:${WT[w]};font-size:${sc(s).toFixed(2)}px;`,
    mono: (s, w = 'regular') => `font-family:Plex;font-weight:${WT[w]};font-size:${sc(s).toFixed(2)}px;`,
    serif: (s, w = 'bold') => `font-family:Serif4;font-weight:${WT[w]};font-size:${sc(s).toFixed(2)}px;`,
  };

  // ---------------------------------------------------------------- math
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const seg = (t, a, b) => clamp((t - a) / (b - a));
  const lerp = (a, b, f) => a + (b - a) * f;
  const easeOut = f => 1 - Math.pow(1 - f, 3);
  const easeInOut = f => f < 0.5 ? 4 * f * f * f : 1 - Math.pow(-2 * f + 2, 3) / 2;
  const iosPush = f => 1 - Math.pow(1 - f, 4);   // UINavigationController push feel
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // ---------------------------------------------------------------- data → model
  let D = null, M = null, SCENE = null;
  const MON = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  const DOW = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];

  // DocumentModel.money: "$9,200", cents only when present.
  function money(dollars) {
    const cents = Math.round(dollars * 100);
    const whole = cents % 100 === 0;
    return '$' + (cents / 100).toLocaleString('en-US', { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 });
  }

  function derive(d) {
    const [y, mo, da] = d.date.split('-').map(Number);
    const dt = new Date(Date.UTC(y, mo - 1, da));
    const dow = DOW[dt.getUTCDay()], mon = MON[mo - 1], dd = String(da).padStart(2, '0');
    const items = [], rows = [];
    let total = 0, hours = 0, gaps = 0;
    const lines = d.lineItems.map((li, i) => {
      const ids = [];
      ids.push(items.length); items.push({ kind: 'plain', tag: 'ITEM', text: li.work, line: i });
      for (const m of li.materials || []) {
        ids.push(items.length); items.push({ kind: 'green', tag: 'PRICE', text: m.spoken, line: i });
      }
      for (const m of li.materials || []) {
        rows.push({ title: m.title, sub: m.detail || '', qty: m.qty || '', amount: money(m.amount), gap: false });
        total += m.amount;
      }
      if (li.hours) {
        hours += li.hours;
        if (li.rate) {
          rows.push({ title: li.laborTitle, sub: li.laborDetail || '', qty: `${li.hours} HR`, amount: money(li.hours * li.rate), gap: false });
          total += li.hours * li.rate;
        } else {
          // MurmurEngine.row: a gap line's detail is replaced by OperatorNote.gap, amount "——".
          rows.push({ title: li.laborTitle, sub: 'NOT HEARD — TAP TO FILL IN', warn: true, qty: `${li.hours} HR`, amount: '——', gap: true });
          gaps++;
        }
      }
      return { said: li.said, ids };
    });
    return {
      items, rows, lines, total, hours, gaps,
      transcript: d.lineItems.map(l => l.said).join(' '),
      boardDate: `${dow} — ${mon} ${dd}`,                        // AppModel.boardDateLabel "EEE — MMM dd"
      notesDate: `${dow}, ${mon} ${da}`,                          // NotesView.metaRight weekday/month/day
      docDate: `${mon} ${dd} ${y}`,                               // letterheadDate "MMM dd yyyy"
    };
  }

  // ---------------------------------------------------------------- icons
  const ICON = {
    camera: (s = 22, c = 'currentColor') => `<svg width="${s}" height="${s * 0.82}" viewBox="0 0 24 19.5" fill="none" stroke="${c}" stroke-width="1.55" stroke-linejoin="round"><path d="M8.3 3.2l1.3-1.9c.2-.3.5-.4.8-.4h3.2c.3 0 .6.1.8.4l1.3 1.9h3.6A2.2 2.2 0 0 1 21.5 5.4v10.4a2.2 2.2 0 0 1-2.2 2.2H4.7a2.2 2.2 0 0 1-2.2-2.2V5.4a2.2 2.2 0 0 1 2.2-2.2z"/><circle cx="12" cy="10.4" r="3.9"/></svg>`,
    chevDown: (s = 9, c = 'currentColor') => `<svg width="${s}" height="${s * 0.6}" viewBox="0 0 10 6" fill="none" stroke="${c}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l4 4 4-4"/></svg>`,
    chevRight: (s = 9, c = 'currentColor') => `<svg width="${s * 0.6}" height="${s}" viewBox="0 0 6 10" fill="none" stroke="${c}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M1 1l4 4-4 4"/></svg>`,
    plus: (s = 11, c = 'currentColor') => `<svg width="${s}" height="${s}" viewBox="0 0 12 12" fill="none" stroke="${c}" stroke-width="2.1" stroke-linecap="round"><path d="M6 1v10M1 6h10"/></svg>`,
  };

  // ---------------------------------------------------------------- chrome
  function statusBar(island = true) {
    return `<div class="abs" style="height:${SAFE_TOP}px;pointer-events:none;z-index:50">
      <div class="sys" style="position:absolute;left:0;width:132px;top:16px;text-align:center;font-size:17px;font-weight:600;letter-spacing:-0.2px;color:#000">${esc(D.clock || '9:41')}</div>
      ${island ? `<div style="position:absolute;left:${(W - 125) / 2}px;top:11px;width:125px;height:37px;border-radius:19px;background:#000"></div>` : ''}
      <div style="position:absolute;right:28px;top:20px;display:flex;align-items:center;gap:6px">
        <svg width="19" height="12" viewBox="0 0 19 12"><rect x="0" y="8" width="3.2" height="4" rx="1" fill="#000"/><rect x="5.2" y="5.5" width="3.2" height="6.5" rx="1" fill="#000"/><rect x="10.4" y="3" width="3.2" height="9" rx="1" fill="#000"/><rect x="15.6" y="0" width="3.2" height="12" rx="1" fill="#000"/></svg>
        <svg width="17" height="12" viewBox="0 0 17 12"><path d="M8.5 2.3c2.5 0 4.8 1 6.5 2.6l1.2-1.2C14.2 1.7 11.5.6 8.5.6S2.8 1.7.8 3.7L2 4.9c1.7-1.6 4-2.6 6.5-2.6z" fill="#000"/><path d="M8.5 5.6c1.6 0 3 .6 4.1 1.6l1.2-1.2c-1.4-1.3-3.3-2.1-5.3-2.1s-3.9.8-5.3 2.1l1.2 1.2c1.1-1 2.5-1.6 4.1-1.6z" fill="#000"/><path d="M8.5 8.9c.7 0 1.3.3 1.8.7L8.5 11.4 6.7 9.6c.5-.4 1.1-.7 1.8-.7z" fill="#000"/></svg>
        <svg width="27" height="13" viewBox="0 0 27 13"><rect x="0.5" y="0.5" width="22" height="12" rx="3.8" fill="none" stroke="#000" stroke-opacity="0.38"/><rect x="2" y="2" width="19" height="9" rx="2.5" fill="#000"/><path d="M24 4.5v4c.8-.3 1.4-1.1 1.4-2s-.6-1.7-1.4-2z" fill="#000" fill-opacity="0.45"/></svg>
      </div>
    </div>`;
  }
  const homeIndicator = () =>
    `<div style="position:absolute;left:${(W - 139) / 2}px;bottom:8px;width:139px;height:5px;border-radius:3px;background:#000;z-index:60"></div>`;

  // A finger: a soft disc where a tap lands, so the edit reads as a person using the phone.
  function tapDot(t, x, y, t0, hold = 0.18) {
    if (!D.showTaps) return '';
    const a = clamp(Math.min(seg(t, t0 - 0.08, t0), 1 - seg(t, t0 + hold, t0 + hold + 0.3)));
    if (a <= 0) return '';
    const s = 1 - 0.12 * seg(t, t0, t0 + hold);
    return `<div style="position:absolute;left:${x - 22}px;top:${y - 22}px;width:44px;height:44px;border-radius:22px;background:rgba(20,20,18,0.22);border:1.5px solid rgba(255,255,255,0.55);opacity:${a};transform:scale(${s});z-index:70"></div>`;
  }
  const press = (t, t0, t1) => (t >= t0 && t < t1) ? 1 : 0;

  // ---------------------------------------------------------------- primitives
  const sectionLabel = (text, color = C.ink60) =>
    `<span style="${F.mono(9, 'semibold')}letter-spacing:2px;color:${color};text-transform:uppercase;white-space:nowrap">${esc(text)}</span>`;

  function sectionHead(left, right, { rightColor = C.ink60, heavy = true, topRule = false, extra = '' } = {}) {
    return `<div class="row" style="padding:10px ${PAD}px 8px;justify-content:space-between;
      border-bottom:${heavy ? 1.5 : 1}px solid ${heavy ? C.ink : C.hairline};${topRule ? `border-top:1.5px solid ${C.ink};` : ''}${extra}">
      ${sectionLabel(left)}${sectionLabel(right, rightColor)}</div>`;
  }

  function fieldTag(kind, label) {
    const col = { red: C.redTag, yellow: C.yellowTag, green: C.greenTag, plain: C.ink60 }[kind];
    const tint = { red: C.redTint, yellow: C.yellowTint, green: C.greenTint, plain: C.paperDeep }[kind];
    return `<span style="${F.mono(8, 'semibold')}letter-spacing:1px;color:${col};background:${tint};padding:3px 6px 2px;line-height:1.25;white-space:nowrap;flex:none">${esc(label)}</span>`;
  }

  function capturedRow(item, style = '', attrs = '') {
    return `<div class="row" ${attrs} style="gap:10px;padding:10px ${PAD}px;border-bottom:1px solid ${C.hairlineSoft};${style}">
      ${fieldTag(item.kind, item.tag)}
      <span class="nowrap" style="${F.cond(13, 'semibold')}flex:0 1 auto;line-height:1.2">${esc(item.text)}</span>
      <span style="flex:1"></span>
      ${item.right ? `<span style="${F.mono(10.5)}color:${C.ink60};white-space:nowrap">${esc(item.right)}</span>` : ''}
    </div>`;
  }

  function metaStrip(left, right, warn = false) {
    return `<div class="row" style="${F.mono(8.5)}letter-spacing:0.4px;color:${C.ink60};padding:7px ${PAD}px;
      border-top:1px solid ${C.hairline};border-bottom:1px solid ${C.hairline};justify-content:space-between;line-height:1.3;white-space:nowrap">
      <span>${esc(left) || '&nbsp;'}</span><span style="${warn ? `font-weight:600;color:${C.yellowTag}` : ''}">${esc(right)}</span></div>`;
  }

  // RaisedBlockStyle: a cap on a 3pt darker lip; pressed = cap travels onto the lip.
  function raised({ label, face = C.orange, lip = C.orangeDeep, text = C.onOrange, border = null, height = 62,
                    radius = 14, dot = false, width = null, grow = false, pressed = 0, disabled = false, padX = 0, style = '' }) {
    const lipH = disabled ? 0 : 3;
    const top = pressed ? 3 : 0;
    const f = disabled ? C.paperDeep : face;
    const b = disabled ? `inset 0 0 0 1px ${C.hairline}` : (border ? `inset 0 0 0 2px ${border}` : 'none');
    const tc = disabled ? C.ink35 : text;
    return `<div style="position:relative;height:${height}px;${width ? `width:${width}px;flex:none;` : ''}${grow ? 'flex:1 1 0;' : ''}
        border-radius:${radius}px;background:${disabled ? 'transparent' : lip};overflow:hidden;${style}">
      ${(!width && !grow) ? `<div class="row" style="visibility:hidden;height:0;overflow:hidden;padding:0 ${padX}px;gap:12px;white-space:nowrap">${dot ? '<span style="width:10px;flex:none"></span>' : ''}${label}</div>` : ''}
      <div class="row" style="position:absolute;left:0;right:0;top:${top}px;height:${height - lipH}px;background:${f};
        border-radius:${radius}px;box-shadow:${b};justify-content:center;gap:12px;color:${tc};padding:0 ${padX}px;white-space:nowrap">
        ${dot && !disabled ? `<span style="width:10px;height:10px;border-radius:5px;background:${tc};flex:none"></span>` : ''}${label}
      </div></div>`;
  }
  const blockLabel = (title, size = 15) => `<span style="${F.ui(size, 'bold')}letter-spacing:1.4px">${esc(title)}</span>`;

  // Controls.swift Waveform: 44 capsules, h = 4 + 22|sin(phase)|, refreshed at 10 Hz.
  function waveform(t) {
    const tq = Math.floor(t * 10) / 10;
    let bars = '';
    for (let i = 0; i < 44; i++) {
      const phase = tq * (1.4 + (i % 7) * 0.33) + i * 0.7;
      const h = 4 + 22 * Math.abs(Math.sin(phase));
      bars += `<span style="width:3px;height:${h.toFixed(2)}px;border-radius:1.5px;background:${C.orange};flex:none"></span>`;
    }
    return `<div class="row" style="height:30px;justify-content:center;gap:2.5px">${bars}</div>`;
  }
  const blink = (t, period, lo) => lo + (1 - lo) * (0.5 + 0.5 * Math.cos(Math.PI * t / period));
  const caret = (t, h = 13) => `<div style="width:2px;height:${h}px;background:${C.orange};opacity:${blink(t, 0.5, 0.15).toFixed(3)}"></div>`;

  // iOS UIActivityIndicatorView (medium): 8 spokes stepping round.
  function spinner(t, color, size = 20) {
    const step = Math.floor(t * 12) % 8;
    let s = '';
    for (let i = 0; i < 8; i++) {
      const o = 0.25 + 0.75 * (((i - step + 8) % 8) / 7);
      s += `<rect x="9" y="1" width="2.4" height="6" rx="1.2" fill="${color}" opacity="${o.toFixed(2)}" transform="rotate(${i * 45} 10.2 10)"/>`;
    }
    return `<svg width="${size}" height="${size}" viewBox="0 0 20.4 20">${s}</svg>`;
  }

  // ---------------------------------------------------------------- screens
  // Each screen is a full 393x852 layer; its own background fills the safe areas, the way
  // `.background(... .ignoresSafeArea())` does in the app.

  function boardScreen(t, st) {
    // BoardView, operator board, fresh: masthead (serif business name; "Your jobs" when the
    // name is blank), date stamp, My business chip, job chips, TODAY / 0 WALKS + EmptyPanel.
    const title = (D.letterhead && D.letterhead.name) ? D.letterhead.name : 'Your jobs';
    return `<div class="abs col" style="background:${C.paper}">
      <div style="height:${SAFE_TOP}px;flex:none"></div>
      <div class="row" style="align-items:flex-start;padding:14px ${PAD}px 12px;gap:12px">
        <div class="col" style="gap:5px;flex:1">
          <div style="${F.serif(28)}line-height:1.15">${esc(title)}</div>
          <div style="${F.mono(10, 'semibold')}letter-spacing:2px;color:${C.amberInk}">${esc(M.boardDate)}</div>
        </div>
        ${raised({ label: `<span style="${F.ui(13, 'semibold')}">My business</span>`, face: C.sheet, lip: C.ink32, text: C.ink, border: C.ink, height: 44, radius: 4, padX: 14 })}
      </div>
      <div class="grow col">
        <div class="row" style="gap:7px;padding:12px ${PAD}px">
          <span class="row" style="${F.ui(13.5, 'semibold')}color:${C.paper};background:${C.ink};border-radius:18px;padding:0 15px;height:36px">All</span>
          <span class="row" style="${F.ui(13.5, 'semibold')}color:${C.amberInk};background:${C.orangeTint};border-radius:18px;padding:0 15px;height:36px;gap:5px">${ICON.plus(11, C.amberInk)}Job</span>
        </div>
        ${sectionHead('TODAY', '0 WALKS', { rightColor: C.amberInk })}
        <div style="padding:16px ${PAD}px 0">
          <div style="${F.ui(14, 'medium')}color:${C.ink60};text-align:center;padding:22px 18px;background:${C.sheet};border-radius:4px;box-shadow:inset 0 0 0 1px ${C.hairline};line-height:1.25">No walks yet. Tap Start walk and talk through the job.</div>
        </div>
      </div>
      <div style="padding:0 ${PAD}px 10px">${raised({ label: blockLabel('START WALK'), dot: true, pressed: st.startPressed })}</div>
      <div style="height:${SAFE_BOTTOM}px;flex:none"></div>
    </div>`;
  }

  function fmtTimer(sec) {
    sec = Math.max(0, Math.floor(sec));
    return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }

  function walkScreen(t, st) {
    // WalkView: RecBanner, MetaStrip, 168pt transcript (anchored bottom) + caret,
    // CAPTURED head, CapturedRows ticking in, Waveform, PHOTO / PAUSE / DONE.
    const shown = st.itemP;          // per-item appear progress 0..1
    const count = shown.filter(p => p > 0).length;
    let rows = '';
    M.items.forEach((it, i) => {
      const p = easeOut(shown[i]);
      rows += capturedRow(it, `opacity:${p.toFixed(3)};transform:translateY(${((1 - p) * 14).toFixed(2)}px)`, `data-fi="${i}"`);
    });
    const followP = shown.reduce((a, p) => a + p, 0);
    return `<div class="abs col" style="background:${C.paper}">
      <div style="height:${SAFE_TOP}px;flex:none"></div>
      <div class="row" style="background:${C.orange};padding:9px ${PAD}px;justify-content:space-between;color:${C.onOrange}">
        <div class="row" style="gap:9px">
          <span style="width:9px;height:9px;border-radius:5px;background:${C.onOrange};opacity:${blink(t, 0.55, 0.25).toFixed(3)}"></span>
          <span style="${F.ui(12, 'bold')}letter-spacing:2.2px;line-height:1.2">RECORDING</span>
        </div>
        <span style="${F.mono(13, 'semibold')}line-height:1.2">${st.timer}</span>
      </div>
      ${metaStrip(D.site || '', st.micStarting ? 'MIC STARTING…' : 'REC — ON-DEVICE STT')}
      <div data-vp="anchor-bottom" style="height:168px;flex:none;overflow:hidden;position:relative">
        <div style="padding:12px ${PAD}px;display:flex;flex-direction:column;gap:6px">
          <div style="${F.mono(11.5)}color:${C.ink60};line-height:${(sc(11.5) * 1.3 + 7).toFixed(2)}px">${esc(st.transcript)}</div>
          ${caret(t)}
        </div>
      </div>
      ${sectionHead('CAPTURED', `${count} ITEMS`, { topRule: true })}
      <div class="grow" data-vp="follow" data-p="${followP.toFixed(4)}" data-margin="0" style="overflow:hidden;position:relative">
        <div>${rows}</div>
      </div>
      <div class="col" style="padding:10px ${PAD}px 10px;gap:12px;border-top:1px solid ${C.hairline}">
        ${waveform(t)}
        <div class="row" style="gap:9px">
          ${raised({ label: `<div class="col" style="align-items:center;gap:3px">${ICON.camera(22, C.ink)}<span style="${F.mono(9, 'semibold')}letter-spacing:1.1px">PHOTO</span></div>`, face: C.sheet, lip: C.ink32, text: C.ink, border: C.ink, width: 62 })}
          ${raised({ label: blockLabel('PAUSE', 13.5), face: C.sheet, lip: C.ink32, text: C.ink, border: C.ink, width: 96 })}
          ${raised({ label: blockLabel('DONE'), face: C.ink, lip: '#000', text: C.paper, grow: true, pressed: st.donePressed, disabled: st.doneDisabled })}
        </div>
      </div>
      <div style="height:${SAFE_BOTTOM}px;flex:none"></div>
    </div>`;
  }

  const SECTION_TITLE = { red: 'NEEDS ATTENTION', yellow: 'FOLLOW-UP', plain: 'SCOPE', green: 'LOOKS GOOD' }; // NotesView.sectionTitle default trade
  const BUCKET_TITLE = { scope_of_work: 'SCOPE OF WORK', constraints: 'CONSTRAINTS', conditions_and_issues: 'CONDITIONS & ISSUES' };

  // NotesView: action bar choices are the live schemas sorted by label (DocumentSchemaModel.buildable).
  const DOC_CHOICES = ['Estimate', 'Invoice', 'Report', 'Work Order'];

  function notesScreen(t, st) {
    const loading = st.loading;
    let body = '';
    if (loading) {
      // NotesView.skeleton
      body += `<div style="margin:14px ${PAD}px 0;height:78px;border-radius:4px;background:${C.paperDeep};filter:brightness(0.975)"></div>`;
      for (let i = 0; i < 3; i++) body += `<div class="row" style="gap:10px;padding:12px ${PAD}px;border-bottom:1px solid ${C.hairline};opacity:0.7">
        <div style="width:44px;height:16px;border-radius:4px;background:#E6E5DE"></div>
        <div class="col" style="gap:5px;flex:1"><div style="height:12px;border-radius:4px;background:#E6E5DE"></div><div style="height:9px;width:180px;border-radius:4px;background:#E6E5DE"></div></div></div>`;
    } else {
      const a = st.contentA;
      body += `<div style="padding:14px ${PAD}px 4px;opacity:${a}"><div class="row" style="align-items:stretch;background:${C.sheet}">
          <div style="width:3px;background:${C.orangeDeep};flex:none"></div>
          <div class="col" style="padding:11px 13px;gap:6px">
            <div style="${F.mono(8, 'semibold')}letter-spacing:2px;color:${C.ink60}">SUMMARY</div>
            <div style="${F.serif(14)}color:${C.ink};line-height:${(sc(14) * 1.28 + 3).toFixed(1)}px">${esc(D.summary)}</div>
          </div></div></div>`;
      // Plan 14 buckets, fixed order, empty ones omitted.
      for (const b of ['scope_of_work', 'constraints', 'conditions_and_issues']) {
        const es = (D.notes || []).filter(n => n.bucket === b);
        if (!es.length) continue;
        body += `<div style="opacity:${a};padding-top:4px">${sectionHead(BUCKET_TITLE[b], String(es.length), { heavy: false })}</div>`;
        for (const e of es) body += `<div class="row" style="align-items:flex-start;gap:9px;padding:8px ${PAD}px;border-bottom:1px solid ${C.hairlineSoft};opacity:${a}">
            <div style="width:5px;height:5px;background:${C.ink35};margin-top:7px;flex:none"></div>
            <div class="col" style="gap:3px"><div style="${F.cond(13.5, 'semibold')}line-height:1.2">${esc(e.label)}</div>
            ${e.detail ? `<div style="${F.cond(11.5, 'medium')}color:${C.ink60};line-height:1.3">${esc(e.detail)}</div>` : ''}</div></div>`;
      }
      // Items grouped attention-first: red, yellow, plain, green.
      for (const k of ['red', 'yellow', 'plain', 'green']) {
        const idx = M.items.map((it, i) => [it, i]).filter(([it]) => it.kind === k);
        if (!idx.length) continue;
        const headA = Math.max(...idx.map(([, i]) => st.itemP[i]));
        body += `<div style="padding-top:4px;opacity:${easeOut(headA).toFixed(3)}">${sectionHead(SECTION_TITLE[k], String(idx.length), { heavy: false })}</div>`;
        for (const [it, i] of idx) {
          const p = easeOut(st.itemP[i]);
          body += capturedRow(it, `opacity:${p.toFixed(3)};transform:translateX(${((1 - p) * 10).toFixed(2)}px)`);
        }
      }
      // ＋ ADD LINE, transcript disclosure.
      body += `<div style="padding:12px ${PAD}px 0;opacity:${a}"><div style="${F.mono(9, 'semibold')}letter-spacing:1px;color:${C.amberInk};text-align:center;padding:11px 0;border:1.5px dashed rgba(154,106,0,0.6);border-radius:4px">＋ ADD LINE</div></div>`;
      body += `<div style="padding:14px ${PAD}px 0;opacity:${a}"><div class="row" style="justify-content:space-between;padding:9px;border:1px dashed ${C.hairline};border-radius:4px">
          <span style="${F.mono(8.5, 'semibold')}letter-spacing:0.8px;color:${C.ink60}">Show the full transcript</span>${ICON.chevRight(11, C.amberInk)}</div></div>`;
      body += `<div style="height:18px"></div>`;
    }

    // Action bar — FILE UNDER, TURN THESE NOTES INTO, doc buttons (hero = first), EXPORT NOTES.
    const btns = DOC_CHOICES.map((label, i) => {
      const hero = i === 0;
      const building = hero && st.building;
      const inner = building ? spinner(t, C.onOrange) : `<span style="${F.ui(12, 'bold')}letter-spacing:0.04px">${label}</span>`;
      return `<div style="flex:none;min-width:88px">${raised({
        label: inner, face: hero ? C.orange : C.sheet, lip: hero ? C.orangeDeep : C.ink32, text: hero ? C.onOrange : C.ink,
        border: hero ? null : C.ink, height: 46, padX: 14, pressed: hero ? st.estPressed : 0,
        disabled: loading || (!hero && st.building), style: building ? 'min-width:88px' : '' })}</div>`;
    }).join('');
    const bar = `<div class="col" style="padding:11px ${PAD}px 10px;gap:9px;background:${C.paper};border-top:1.5px solid ${C.ink};opacity:${loading ? 0.4 : 1}">
      <div class="row" style="gap:8px;justify-content:space-between">
        <span style="${F.mono(8.5, 'semibold')}letter-spacing:1.8px;color:${C.ink60}">FILE UNDER</span>
        <span class="row" style="${F.mono(9, 'semibold')}letter-spacing:0.8px;color:${C.ink60};background:${C.paperDeep};padding:7px 9px;gap:5px">Choose a job${ICON.chevDown(9, C.ink60)}</span>
      </div>
      <span style="${F.mono(8.5, 'semibold')}letter-spacing:1.8px;color:${C.ink60}">TURN THESE NOTES INTO</span>
      <div data-vp="hfrac" data-f="${(st.hscroll || 0).toFixed(4)}" style="overflow:hidden;margin:0 -${PAD}px;padding:3px 0">
        <div class="row" style="gap:8px;width:max-content;padding:0 ${PAD}px">${btns}</div>
      </div>
      <div style="${F.mono(9, 'semibold')}letter-spacing:1px;color:${C.ink60};height:40px;display:flex;align-items:center;justify-content:center;border-radius:4px;box-shadow:inset 0 0 0 1.5px ${C.hairline}">⇪&nbsp;&nbsp;EXPORT NOTES</div>
    </div>`;

    const progress = loading
      ? `<div style="height:2px;background:rgba(154,106,0,0.18);position:relative;overflow:hidden"><div style="position:absolute;top:0;height:2px;width:120px;background:${C.orangeDeep};left:${(((t * 0.9) % 1) * (W + 120) - 120).toFixed(1)}px"></div></div>`
      : `<div style="height:2px;background:${C.paper}"></div>`;
    const right = loading ? 'READING YOUR WALK…' : `${M.notesDate} · ${M.items.length} ITEM${M.items.length === 1 ? '' : 'S'}`;
    return `<div class="abs col" style="background:${C.paperDeep}">
      <div style="height:${SAFE_TOP}px;flex:none"></div>
      ${progress}
      <div class="row" style="justify-content:space-between;padding:12px ${PAD}px 10px;background:${C.paper}">
        <span style="${F.mono(9, 'semibold')}letter-spacing:1px;color:${C.ink60};padding:4px 0">‹ CLOSE</span>
        <span style="${F.mono(9, 'semibold')}letter-spacing:2px;color:${C.amberInk}">WALK NOTES</span>
      </div>
      <div style="background:${C.paper}">${metaStrip((D.letterhead && D.letterhead.name || '').toUpperCase(), right)}</div>
      <div class="grow" data-vp="frac" data-f="${(st.scroll || 0).toFixed(4)}" style="overflow:hidden;position:relative;background:${C.paperDeep}">
        <div>${body}</div>
      </div>
      ${bar}
      <div style="height:${SAFE_BOTTOM}px;flex:none;background:${C.paperDeep}"></div>
    </div>`;
  }

  function docSection(label, inner) {
    return `<div class="col" style="gap:7px;padding-top:14px">
      <div><span style="${F.mono(8, 'semibold')}letter-spacing:1.6px;color:${C.ink60};padding-bottom:1px;border-bottom:1px solid ${C.hairline};display:inline-block">${esc(label)}</span></div>
      ${inner}</div>`;
  }
  const tapToAdd = `<span style="${F.mono(8, 'semibold')}letter-spacing:1px;color:${C.amberInk}">TAP TO ADD</span>`;
  const inlineField = (label, value) => `<div class="row" style="align-items:baseline;gap:10px">
      <span style="${F.mono(7.5, 'semibold')}letter-spacing:1.2px;color:${C.ink45};width:104px;flex:none">${esc(label)}</span>${value}</div>`;

  function docRow(r, style = '', attrs = '') {
    const amount = r.gap
      ? `<span style="${F.mono(12, 'semibold')}color:${C.yellowTag};text-decoration:underline dashed ${C.yellowTag};text-underline-offset:3px">${r.amount}</span>`
      : `<span style="${F.mono(12, 'semibold')}color:${C.ink}">${esc(r.amount)}</span>`;
    const sub = r.sub ? (r.warn
      ? `<div style="${F.mono(8, 'semibold')}letter-spacing:0.3px;color:${C.yellowTag};line-height:1.3">${esc(r.sub)}</div>`
      : `<div style="${F.ui(11, 'medium')}color:${C.ink60};line-height:1.25">${esc(r.sub)}</div>`) : '';
    return `<div class="row" ${attrs} style="align-items:baseline;gap:10px;padding:11px 0;border-bottom:1px solid ${C.hairlineSoft};${style}">
      <div class="col" style="gap:2px;flex:1;min-width:0"><div style="${F.cond(12.5, 'semibold')}line-height:1.2">${esc(r.title)}</div>${sub}</div>
      ${r.qty ? `<span style="${F.mono(9.5)}color:${C.ink60};white-space:nowrap">${esc(r.qty)}</span>` : ''}
      ${amount}
    </div>`;
  }

  function reviewScreen(t, st) {
    // ReviewView + Document.swift: header ‹ NOTES / kind, the sheet (letterhead, schema sections,
    // lines, ADD LINE, TotalRow + GapChip, RevNote, photo well), DISCARD + SEND ESTIMATE.
    const lh = D.letterhead || {};
    const A = (x) => `opacity:${easeOut(x).toFixed(3)};transform:translateY(${((1 - easeOut(x)) * 8).toFixed(2)}px);`;
    const letterhead = `<div class="row" style="align-items:flex-start;gap:12px;padding-bottom:12px;border-bottom:2px solid ${C.ink};${A(st.headA)}">
        <div class="col" style="gap:4px;flex:1">
          ${lh.name ? `<div style="${F.serif(15)}line-height:1.2">${esc(lh.name)}</div>` : ''}
          ${lh.sub ? `<div style="${F.mono(7.5)}letter-spacing:0.6px;color:${C.ink60}">${esc(lh.sub)}</div>` : ''}
        </div>
        <div class="col" style="align-items:flex-end;gap:3px;white-space:nowrap">
          <span style="${F.mono(8.5, 'semibold')}letter-spacing:1.8px;color:${C.orangeDeep}">ESTIMATE</span>
          <span style="${F.mono(11, 'semibold')}">${esc(D.docNumber || 'EST-0001')}</span>
          <span style="${F.mono(8)}color:${C.ink60}">${M.docDate}</span>
        </div></div>`;
    // Estimate schema (domain.rs builtin_schemas): client, scope, validity, tax, acceptance.
    // Section stamps come from MurmurEngine.sectionLabel(key).
    const sections = `<div style="${A(st.secA)}">
        ${docSection('CLIENT', tapToAdd)}
        ${docSection('SCOPE OF WORK', `<div style="${F.ui(12, 'medium')}line-height:${(sc(12) * 1.2 + 2).toFixed(1)}px">${esc(D.scope)}</div>`)}
        ${docSection('VALIDITY', inlineField('VALID UNTIL', tapToAdd))}
        ${docSection('TAX', inlineField('TAX', tapToAdd))}
        ${docSection('ACCEPTANCE', inlineField('ACCEPTED BY', tapToAdd))}
        <div style="height:14px"></div></div>`;
    let rows = '';
    M.rows.forEach((r, i) => { rows += docRow(r, A(st.rowP[i]), `data-fi="${i}"`); });
    const n = M.rows.length;
    const addLine = `<div data-fi="${n}" style="padding-top:10px;${A(st.totA)}"><span class="row" style="display:inline-flex;gap:5px;${F.mono(9, 'semibold')}letter-spacing:1.2px;color:${C.amberInk};padding:7px 9px;border-radius:4px;box-shadow:inset 0 0 0 1px rgba(154,106,0,0.45)">${ICON.plus(11, C.amberInk)}ADD LINE</span></div>`;
    const totalVal = M.total > 0 ? money(M.total) : '——';
    const total = `<div data-fi="${n + 1}" class="row" style="align-items:baseline;justify-content:space-between;margin-top:2px;padding-top:10px;border-top:2px solid ${C.ink};${A(st.totA)}">
        <span style="${F.mono(9, 'semibold')}letter-spacing:2px">TOTAL</span>
        <span class="row" style="align-items:baseline;gap:8px">
          <span style="${F.mono(totalVal.length > 9 ? 11.5 : 17, 'semibold')}">${totalVal}</span>
          ${M.gaps > 0 ? `<span style="${F.mono(8.5, 'semibold')}letter-spacing:0.9px;color:${C.yellowTag};background:${C.yellowTint};padding:3px 6px 2px;opacity:${easeOut(st.chipA).toFixed(3)}">+${M.gaps} GAP</span>` : ''}
        </span></div>`;
    const note = `<div data-fi="${n + 2}" style="padding-top:10px;${A(st.noteA)}"><div class="row" style="align-items:stretch;background:${C.yellowTint}">
        <div style="width:3px;background:${C.yellowTag};flex:none"></div>
        <div style="${F.mono(8)}letter-spacing:0.4px;color:${C.ink60};padding:6px 9px;line-height:1.3">GAPS ARE MARKED — TAP TO FILL BEFORE SENDING</div></div></div>`;
    const photos = `<div data-fi="${n + 3}" class="col" style="padding-top:14px;gap:10px;${A(st.noteA)}">
        <div class="row" style="gap:8px;align-items:baseline">${sectionLabel('PHOTOS')}<span style="${F.mono(9, 'semibold')}color:${C.ink60}">× 0</span><span style="flex:1"></span>
          <span style="${F.mono(9, 'semibold')}letter-spacing:1.2px;color:${C.amberInk};padding:5px 8px;border-radius:4px;box-shadow:inset 0 0 0 1.5px ${C.orangeDeep}">+ ADD</span></div>
        <div style="${F.mono(8)}letter-spacing:0.6px;color:${C.ink45};text-align:center;padding:18px 10px;border:1px dashed ${C.ink45};border-radius:4px;line-height:1.3">No photos yet. Take them during a walk, or add some here.</div></div>`;
    const sheet = `<div style="margin:14px 14px 20px;padding:18px;background:${C.sheet};box-shadow:0 1px 1px rgba(20,20,18,0.12),0 10px 14px rgba(20,20,18,0.14)">
        ${letterhead}${sections}${rows}${addLine}${total}${note}${photos}</div>`;
    const followP = st.followP;
    return `<div class="abs col" style="background:${C.paperDeep}">
      <div style="height:${SAFE_TOP}px;flex:none"></div>
      <div class="row" style="justify-content:space-between;padding:12px ${PAD}px 10px;background:${C.paper}">
        <span style="${F.mono(9, 'semibold')}letter-spacing:1px;color:${C.ink60};padding:4px 0">‹ NOTES</span>
        <span style="${F.mono(9, 'semibold')}letter-spacing:2px;color:${C.amberInk}">ESTIMATE</span>
      </div>
      <div class="grow" data-vp="follow" data-p="${followP.toFixed(4)}" data-margin="${st.followMargin || 24}" data-base="${st.followBase || 0}" style="overflow:hidden;position:relative;background:${C.paperDeep}">
        <div>${sheet}</div>
      </div>
      <div class="row" style="gap:10px;padding:12px ${PAD}px 10px;background:${C.paper};border-top:1px solid ${C.hairline}">
        ${raised({ label: blockLabel('DISCARD', 14), face: C.sheet, lip: C.ink32, text: C.ink, border: C.ink, height: 58, width: 124 })}
        ${raised({ label: blockLabel('SEND ESTIMATE'), height: 58, grow: true, pressed: st.sendPressed })}
      </div>
      <div style="height:${SAFE_BOTTOM}px;flex:none;background:${C.paper}"></div>
    </div>`;
  }

  // ReviewView.confirmationDialog — iOS action sheet, app tint = Theme.C.ink (GalleryApp `.tint`).
  function gapDialog(t, st) {
    const p = st.dialogP;
    if (p <= 0) return '';
    const y = (1 - iosPush(p)) * 330;
    const msg = M.gaps === 1 ? 'One line has no price on it. Send anyway?' : `${M.gaps} lines have no price on them. Send anyway?`;
    return `<div class="abs" style="background:rgba(0,0,0,${(0.2 * p).toFixed(3)});z-index:30"></div>
      <div class="sys" style="position:absolute;left:8px;right:8px;bottom:${SAFE_BOTTOM}px;transform:translateY(${y.toFixed(1)}px);z-index:31">
        <div style="border-radius:14px;overflow:hidden;background:rgba(242,242,242,0.86);backdrop-filter:blur(30px) saturate(1.8)">
          <div style="padding:15px 16px 14px;text-align:center;font-size:13px;font-weight:600;color:rgba(60,60,67,0.6);letter-spacing:-0.08px;line-height:18px">${esc(msg)}</div>
          <div style="height:0.5px;background:rgba(60,60,67,0.29)"></div>
          <div style="height:57px;display:flex;align-items:center;justify-content:center;font-size:20px;color:${C.ink};letter-spacing:-0.4px;background:${st.anywayDown ? 'rgba(0,0,0,0.09)' : 'transparent'}">Send anyway</div>
        </div>
        <div style="margin-top:8px;height:57px;border-radius:14px;background:#fff;display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:600;color:${C.ink};letter-spacing:-0.4px">Go back and fill it in</div>
      </div>`;
  }

  // ShareSheet = UIActivityViewController over the rendered PDF (DocumentPDF.swift; file "EST-0001.pdf").
  function shareSheet(t, st) {
    const p = st.shareP;
    if (p <= 0) return '';
    const top = 352;
    const y = (1 - iosPush(p)) * (H - top);
    const app = (bg, glyph, label) => `<div class="col" style="align-items:center;gap:7px;width:66px">
        <div style="width:62px;height:62px;border-radius:14px;background:${bg};display:flex;align-items:center;justify-content:center">${glyph}</div>
        <span style="font-size:11px;color:#000;letter-spacing:0.06px">${label}</span></div>`;
    const action = (label, icon, last) => `<div style="height:50px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;font-size:17px;color:#000;letter-spacing:-0.4px;${last ? '' : 'border-bottom:0.5px solid rgba(60,60,67,0.29)'}">${label}${icon}</div>`;
    const ico = d => `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
    const thumb = `<div style="width:48px;height:60px;background:#fff;border-radius:3px;box-shadow:0 0 0 0.5px rgba(0,0,0,0.18),0 1px 3px rgba(0,0,0,0.12);padding:5px 5px;display:flex;flex-direction:column;gap:2.5px">
        <div style="display:flex;justify-content:flex-end"><div style="width:14px;height:2px;background:${C.orangeDeep}"></div></div>
        <div style="height:1px;background:#141412;margin:1px 0"></div>
        ${Array.from({ length: 9 }, (_, i) => `<div style="height:1.6px;width:${[90, 60, 80, 70, 85, 55, 75, 65, 50][i]}%;background:${i % 2 ? '#C9A64F' : '#bbb'}"></div>`).join('')}</div>`;
    return `<div class="abs" style="background:rgba(0,0,0,${(0.22 * p).toFixed(3)});z-index:40"></div>
      <div class="sys" style="position:absolute;left:0;right:0;top:${top}px;bottom:0;transform:translateY(${y.toFixed(1)}px);z-index:41;background:#F2F2F7;border-radius:12px 12px 0 0;overflow:hidden">
        <div class="row" style="gap:12px;padding:18px 16px 16px">
          ${thumb}
          <div class="col" style="flex:1;gap:2px">
            <span style="font-size:15px;font-weight:600;color:#000;letter-spacing:-0.2px">${esc(D.docNumber || 'EST-0001')}</span>
            <span style="font-size:13px;color:rgba(60,60,67,0.6)">PDF Document</span>
          </div>
          <div style="width:30px;height:30px;border-radius:15px;background:rgba(118,118,128,0.12);display:flex;align-items:center;justify-content:center">
            <svg width="11" height="11" viewBox="0 0 12 12" stroke="rgba(60,60,67,0.6)" stroke-width="2.2" stroke-linecap="round"><path d="M2 2l8 8M10 2l-8 8"/></svg></div>
        </div>
        <div style="height:0.5px;background:rgba(60,60,67,0.29)"></div>
        <div class="row" style="gap:12px;padding:20px 14px 18px;align-items:flex-start">
          ${app('linear-gradient(#5AC8FA,#0A84FF)', `<svg width="38" height="38" viewBox="0 0 40 40" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round"><path d="M9 27a14 14 0 1 1 22 0"/><path d="M14 23.5a8 8 0 1 1 12 0"/><path d="M20 21v12"/><path d="M15 31l5 5 5-5" stroke-linejoin="round"/></svg>`, 'AirDrop')}
          ${app('linear-gradient(#6EE36E,#2AC33E)', `<svg width="38" height="34" viewBox="0 0 40 36"><path d="M20 3C10.6 3 3 9.3 3 17c0 4.3 2.3 8.2 6 10.8-.3 2.4-1.5 4.6-3.3 6.2 3.4 0 6.6-1.2 9-3.2 1.7.5 3.5.7 5.3.7 9.4 0 17-6.3 17-14S29.4 3 20 3z" fill="#fff"/></svg>`, 'Messages')}
          ${app('linear-gradient(#4FB4FF,#1E7CF2)', `<svg width="38" height="28" viewBox="0 0 40 30"><rect x="2" y="2" width="36" height="26" rx="3" fill="#fff"/><path d="M3 4l17 13L37 4" fill="none" stroke="#1E7CF2" stroke-width="2"/></svg>`, 'Mail')}
          ${app('#fff', `<svg width="38" height="30" viewBox="0 0 40 32"><path d="M2 6a3 3 0 0 1 3-3h10l3 4h17a3 3 0 0 1 3 3v17a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3z" fill="#1E90FF"/><path d="M2 12h36v15a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3z" fill="#4DB0FF"/></svg>`, 'Files')}
          ${app('linear-gradient(#FFE27A,#FFCC00)', `<svg width="34" height="34" viewBox="0 0 36 36"><rect x="6" y="5" width="24" height="26" rx="2" fill="#fff"/><path d="M10 13h16M10 18h16M10 23h11" stroke="#C9A100" stroke-width="1.8"/></svg>`, 'Notes')}
        </div>
        <div style="margin:0 16px;border-radius:10px;background:#fff">
          ${action('Copy', ico('<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>'))}
          ${action('Save to Files', ico('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'))}
          ${action('Print', ico('<path d="M7 9V3h10v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M7 14h10v7H7z"/>'), true)}
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------- layout fix-ups (measured scroll)
  function fixups(root) {
    root.querySelectorAll('[data-vp]').forEach(vp => {
      const inner = vp.firstElementChild;
      const vh = vp.clientHeight, ih = inner.scrollHeight;
      const mode = vp.dataset.vp;
      if (mode === 'anchor-bottom') {
        inner.style.transform = `translateY(${Math.min(0, vh - ih)}px)`;
      } else if (mode === 'frac') {
        inner.style.transform = `translateY(${-Math.max(0, ih - vh) * parseFloat(vp.dataset.f)}px)`;
      } else if (mode === 'hfrac') {
        const vw = vp.clientWidth, iw = inner.scrollWidth;
        inner.style.transform = `translateX(${-Math.max(0, iw - vw) * parseFloat(vp.dataset.f)}px)`;
      } else if (mode === 'follow') {
        // Keep the newest revealed element's bottom in view; p is fractional (eased in between).
        const p = parseFloat(vp.dataset.p), margin = parseFloat(vp.dataset.margin || 0);
        const els = [...inner.querySelectorAll('[data-fi]')].sort((a, b) => a.dataset.fi - b.dataset.fi);
        const top0 = inner.getBoundingClientRect().top;
        const bottoms = els.map(e => { const r = e.getBoundingClientRect(); return r.top - top0 + r.height - (parseFloat(getComputedStyle(e).transform.split(',')[5]) || 0); });
        const first = els.length ? els[0].getBoundingClientRect().top - top0 : 0;
        const b = k => k <= 0 ? first : bottoms[Math.min(k, bottoms.length) - 1];
        const n = Math.floor(p), f = p - n;
        const bottom = lerp(b(n), b(n + 1), easeOut(f));
        const s = clamp(bottom + margin - vh, 0, Math.max(0, ih - vh));
        inner.style.transform = `translateY(${-s}px)`;
      }
    });
  }

  // ---------------------------------------------------------------- scene timelines
  function walkTimeline() {
    // Lines share 2.0–10.6 s in proportion to their words; ITEM lands ~55% in, PRICEs near the end.
    const t0 = 2.0, t1 = 10.6;
    const words = M.lines.map(l => l.said.split(/\s+/).length);
    const tot = words.reduce((a, b) => a + b, 0);
    let acc = t0;
    const spans = M.lines.map((l, i) => { const a = acc; acc += (t1 - t0) * words[i] / tot; return [a, acc]; });
    const appear = new Array(M.items.length).fill(Infinity);
    M.lines.forEach((l, i) => {
      const [a, b] = spans[i];
      l.ids.forEach((id, k) => {
        appear[id] = k === 0 ? lerp(a, b, 0.55) : lerp(a, b, 0.78 + 0.2 * (k - 1) / Math.max(1, l.ids.length - 1));
      });
    });
    return { spans, appear };
  }
  function transcriptAt(t, tl) {
    let out = [];
    M.lines.forEach((l, i) => {
      const [a, b] = tl.spans[i];
      const ws = l.said.split(/\s+/);
      const k = Math.floor(ws.length * seg(t, a, b));
      if (k > 0) out.push(ws.slice(0, k).join(' '));
    });
    return out.join(' ');
  }
  function walkState(t, tl, startAt = 1.3) {
    return {
      timer: fmtTimer(t - startAt),
      micStarting: t < 1.9,
      transcript: transcriptAt(t, tl),
      itemP: tl.appear.map(a => seg(t, a, a + 0.3)),
      doneDisabled: t < tl.spans[0][0] + 0.2,
    };
  }

  function notesItemsP(t, base) {
    // The SCOPE rows (work) land first, then the PRICE rows: the operator watching the notes fill.
    const order = [...M.items.keys()].sort((a, b) => (M.items[a].kind === 'green') - (M.items[b].kind === 'green') || a - b);
    const p = new Array(M.items.length).fill(0);
    let k = 0;
    for (const i of order) {
      const isPrice = M.items[i].kind === 'green';
      const at = base + (isPrice ? 1.65 + 0.24 * (k - M.lines.length) : 0.3 + 0.3 * k);
      p[i] = seg(t, at, at + 0.3); k++;
    }
    return p;
  }
  const NOTES_FINAL_SCROLL = 1;

  const SCENES = {
    walk: {
      dur: 12, render(t) {
        const tl = walkTimeline();
        const push = iosPush(seg(t, 1.05, 1.5));
        let h = '';
        if (push < 1) h += `<div class="abs" style="transform:translateX(${(-push * W * 0.3).toFixed(1)}px)">${boardScreen(t, { startPressed: press(t, 0.85, 1.0) })}<div class="abs" style="background:rgba(0,0,0,${(0.1 * push).toFixed(3)})"></div></div>`;
        if (t >= 1.05) h += `<div class="abs" style="transform:translateX(${((1 - push) * W).toFixed(1)}px);box-shadow:-6px 0 18px rgba(0,0,0,${(0.12 * (1 - push)).toFixed(3)})">${walkScreen(t, { ...walkState(t, tl), donePressed: press(t, 11.25, 11.42) })}</div>`;
        h += tapDot(t, W / 2, H - SAFE_BOTTOM - 10 - 31, 0.85, 0.15);
        h += tapDot(t, 288, H - SAFE_BOTTOM - 10 - 31, 11.25, 0.17);
        return h;
      },
    },
    notes: {
      dur: 9.5, render(t) {
        const tl = walkTimeline();
        const push = iosPush(seg(t, 0.2, 0.62));
        const loaded = 2.0;
        let h = '';
        if (push < 1) h += `<div class="abs" style="transform:translateX(${(-push * W * 0.3).toFixed(1)}px)">${walkScreen(t + 11.2, { ...walkState(11.5, tl), donePressed: press(t, 0.0, 0.17) })}<div class="abs" style="background:rgba(0,0,0,${(0.1 * push).toFixed(3)})"></div></div>`;
        if (t >= 0.2) h += `<div class="abs" style="transform:translateX(${((1 - push) * W).toFixed(1)}px)">${notesScreen(t, {
          loading: t < loaded, contentA: easeOut(seg(t, loaded, loaded + 0.25)).toFixed(3),
          itemP: notesItemsP(t, loaded), scroll: easeInOut(seg(t, 5.7, 7.4)) * NOTES_FINAL_SCROLL })}</div>`;
        h += tapDot(t, 288, H - SAFE_BOTTOM - 10 - 31, 0.0, 0.17);
        return h;
      },
    },
    chooser: {
      dur: 7.5, render(t) {
        const push = iosPush(seg(t, 5.2, 5.65));
        const hs = easeInOut(seg(t, 0.8, 2.0)) * (1 - easeInOut(seg(t, 2.5, 3.3)));
        let h = '';
        const all = new Array(M.items.length).fill(1);
        if (push < 1) h += `<div class="abs" style="transform:translateX(${(-push * W * 0.3).toFixed(1)}px)">${notesScreen(t, {
          loading: false, contentA: 1, itemP: all, scroll: NOTES_FINAL_SCROLL, hscroll: hs,
          estPressed: press(t, 3.8, 3.95), building: t >= 3.95 })}<div class="abs" style="background:rgba(0,0,0,${(0.1 * push).toFixed(3)})"></div></div>`;
        if (t >= 5.2) h += `<div class="abs" style="transform:translateX(${((1 - push) * W).toFixed(1)}px)">${reviewScreen(t, fullDoc(0))}</div>`;
        // hero Estimate button center: x = 20 + width/2, y = bottom bar
        h += tapDot(t, 64, H - SAFE_BOTTOM - 10 - 40 - 9 - 26, 3.8, 0.15);
        return h;
      },
    },
    estimate: {
      dur: 9.5, render(t) {
        const n = M.rows.length;
        const r0 = 1.1, dr = 0.3;
        const rowP = M.rows.map((_, i) => seg(t, r0 + i * dr, r0 + i * dr + 0.35));
        const tEnd = r0 + n * dr + 0.15;
        const totA = seg(t, tEnd, tEnd + 0.35), chipA = seg(t, tEnd + 0.3, tEnd + 0.6), noteA = seg(t, tEnd + 0.5, tEnd + 0.85);
        // follow: rows revealed so far (+ add line/total/note), eased
        const followP = rowP.reduce((a, b) => a + b, 0) + totA * 2 + noteA;
        return reviewScreen(t, { headA: seg(t, 0.15, 0.5), secA: seg(t, 0.45, 0.85), rowP, totA, chipA, noteA, followP, followMargin: 16 });
      },
    },
    send: {
      dur: 9, render(t) {
        const st = fullDoc(1);
        st.sendPressed = press(t, 0.8, 0.95);
        let h = reviewScreen(t, st);
        h += gapDialog(t, { dialogP: seg(t, 1.0, 1.4) * (1 - seg(t, 4.1, 4.4)), anywayDown: t >= 3.9 && t < 4.1 });
        h += shareSheet(t, { shareP: seg(t, 4.45, 4.95) });
        h += tapDot(t, 258, H - SAFE_BOTTOM - 10 - 29, 0.8, 0.15);
        h += tapDot(t, W / 2, H - SAFE_BOTTOM - 57 - 8 - 28, 3.9, 0.15);
        return h;
      },
    },
  };
  function fullDoc(end) {
    // Fully built document; end=1 scrolls to the total/gap note (the estimate scene's last frame).
    const n = M.rows.length;
    return { headA: 1, secA: 1, rowP: new Array(n).fill(1), totA: 1, chipA: 1, noteA: 1,
             followP: end ? n + 3 : 0, followMargin: 16 };
  }

  // ---------------------------------------------------------------- API
  function boot(data, scene) {
    D = Object.assign({ showTaps: true, island: true }, data);
    M = derive(D);
    SCENE = SCENES[scene];
    if (!SCENE) throw new Error('unknown scene ' + scene);
    // Load every face up front: fonts.ready alone resolves before any text has asked for them,
    // and the first frame would then be measured with fallback metrics.
    const faces = ['400 12px Barlow', '500 12px Barlow', '600 12px Barlow', '700 12px Barlow', '800 12px Barlow',
      '500 12px BarlowSC', '600 12px BarlowSC', '400 12px Plex', '500 12px Plex', '600 12px Plex',
      '600 12px Serif4', '700 12px Serif4'];
    return Promise.all(faces.map(f => document.fonts.load(f))).then(() => document.fonts.ready).then(() => SCENE.dur);
  }
  function renderAt(t) {
    const root = document.getElementById('screen');
    root.innerHTML = SCENE.render(t) + statusBar(D.island) + homeIndicator();
    fixups(root);
    return true;
  }
  return { boot, renderAt, duration: () => SCENE.dur, scenes: Object.keys(SCENES), model: () => M };
})();
