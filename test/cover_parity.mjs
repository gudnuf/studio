// Reads {cases: [[cover, s1, s2, pace, cin], ...]} on stdin, prints ui/ops.js coverCut of each
// as JSON: the JS half of test/cover_parity.py.
import { coverCut } from '../ui/ops.js';
let src = '';
process.stdin.on('data', d => { src += d; });
process.stdin.on('end', () => {
  const { cases } = JSON.parse(src);
  process.stdout.write(JSON.stringify(cases.map(([cover, s1, s2, pace, cin]) => coverCut(cover, s1, s2, pace, cin))));
});
