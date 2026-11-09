// "Format" = aspect ratio, in words a creator uses.
export const FORMATS = {
  '9:16': { w: 9, h: 16, name: 'Vertical', apps: 'Reels · TikTok · Shorts' },
  '16:9': { w: 16, h: 9, name: 'Wide', apps: 'YouTube · websites' },
  '1:1': { w: 1, h: 1, name: 'Square', apps: 'Feed posts' },
  '4:5': { w: 4, h: 5, name: 'Portrait', apps: 'Instagram & LinkedIn feed' },
};
export const ORDER = ['9:16', '16:9', '1:1', '4:5'];

export function formatIcon(aspect, size = 18) {
  const f = FORMATS[aspect] || FORMATS['9:16'];
  const k = size / Math.max(f.w, f.h);
  const w = f.w * k, h = f.h * k;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><rect x="${(size - w) / 2 + 1}" y="${(size - h) / 2 + 1}" width="${w - 2}" height="${h - 2}" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
}

// Export pixel sizes per aspect (studio.py ASPECTS).
export const PIXELS = { '9:16': [1080, 1920], '16:9': [1920, 1080], '1:1': [1080, 1080], '4:5': [1080, 1350] };

/** How a clip fills the frame when nobody chose (studio.py fit_default is the same rule):
 *  'fill' crops to cover (camera footage); 'fit' shows the whole frame over a blurred copy.
 *  A silent portrait source whose shape is far from the target (a screen recording) gets 'fit'. */
export function fitDefault(m, aspect) {
  const w = m?.width || 0, h = m?.height || 0, px = PIXELS[aspect];
  if (!m?.has_video || !w || !h || !px) return 'fill';
  const far = Math.abs(Math.log((w / h) / (px[0] / px[1]))) > Math.log(1.15);
  return !m.has_audio && h > w && far ? 'fit' : 'fill';
}
export const clipFit = (c, m, aspect) => (c?.fit === 'fill' || c?.fit === 'fit') ? c.fit : fitDefault(m, aspect);
export const FIT_WORDS = { fill: 'Fill the frame', fit: 'Show everything' };
