// The Mac app (Tauri), when the UI is running inside it; `null` in a browser. Everything
// native goes through this one object, so the rest of the UI only asks `if (native)` and the
// browser path (`./studio ui` in Chrome) stays exactly as it was.
//
// What the app adds: drops arrive as real file paths (no upload; the server clones them into
// the library), File ▸ Add Footage… is a native picker, the menu bar sends commands here, and
// a notification goes out when something finishes while the window is in the background.

const T = window.__TAURI__;

export const native = T?.core?.invoke && T?.event?.listen ? {
  /** The native file picker; resolves to absolute paths ([] if cancelled). */
  pickFootage: () => T.core.invoke('pick_footage').catch(() => []),
  /** The native folder picker (Share / Import); resolves to an absolute path, or null. */
  pickFolder: title => T.core.invoke('pick_folder', { title }).catch(() => null),
  /** A notification, only if the window isn't in front (the app checks). */
  notify: (title, body) => T.core.invoke('notify', { title, body }).catch(() => false),
  info: () => T.core.invoke('app_info').catch(() => null),
  /** Menu bar commands: fn(id), e.g. 'undo', 'export', 'add-footage'. */
  onMenu: fn => T.event.listen('studio://menu', e => fn(e.payload)),
  /** File drags from Finder: fn({type: 'enter'|'drop'|'leave', paths}). */
  onDrag: fn => T.event.listen('studio://drag', e => fn(e.payload || {})),
} : null;

if (native) document.documentElement.classList.add('native');
