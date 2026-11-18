export function validProbabilities(p) {
  return Array.isArray(p) && p.length === 3 &&
    p.every(v => Number.isFinite(v) && v >= 0 && v <= 1) &&
    Math.abs(p.reduce((a, b) => a + b, 0) - 1) <= 1e-8;
}

// Largest remainders, in tenths of a percent. Stored probabilities stay exact.
export function rounded(p) {
  if (!validProbabilities(p)) throw new Error('Некорректные вероятности');
  const sum = p.reduce((a, b) => a + b, 0);
  const raw = p.map(x => x / sum * 1000);
  const units = raw.map(Math.floor);
  const order = [0, 1, 2].sort((a, b) => (raw[b] - units[b]) - (raw[a] - units[a]) || a - b);
  const left = 1000 - units.reduce((a, b) => a + b, 0);
  for (let i = 0; i < left; i++) units[order[i]]++;
  return units.map(v => (v / 10).toFixed(1) + '%');
}

export function topOutcome(p) {
  const max = Math.max(...p);
  const leaders = [0, 1, 2].filter(i => Math.abs(p[i] - max) <= 1e-12);
  return leaders.length === 1 ? leaders[0] : null;
}
