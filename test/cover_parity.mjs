// Reads {cases: [[cover, s1, s2, pace, cin], ...], captions: [[words, segments, start, end, pace],
// ...]} on stdin, prints {cover: ui/ops.js coverCut of each, captions: ui/captions.js
// captionChunks of each} as JSON: the JS half of test/cover_parity.py.
import { coverCut } from '../ui/ops.js';
import { captionChunks } from '../ui/captions.js';
let src = '';
process.stdin.on('data', d => { src += d; });
process.stdin.on('end', () => {
  const { cases, captions } = JSON.parse(src);
  process.stdout.write(JSON.stringify({
    cover: cases.map(([cover, s1, s2, pace, cin]) => coverCut(cover, s1, s2, pace, cin)),
    captions: (captions || []).map(([words, segs, start, end, pace]) => captionChunks(words, segs, start, end, pace)),
  }));
});
