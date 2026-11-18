export const API = 'https://footpredict-api.wzjp5747md.workers.dev';
let packet = null;
let receivedAt = 0;
let pending = null;

export function peekQuality() { return packet; }

export async function loadQuality({refresh = false} = {}) {
  if (pending) return pending;
  if (!refresh && packet && Date.now() - receivedAt < 5 * 60000) return packet;
  pending = (async () => {
    const response = await fetch(API + '/v1/quality', {
      cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(20000)
    });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (data.schemaVersion !== 1 || !Number.isFinite(Date.parse(data.generatedAt)) ||
        !data.cohorts?.v1 || !data.cohorts?.v2 || !data.paired ||
        !Array.isArray(data.fixtures) || data.fixtureScope?.returned !== data.fixtures.length)
      throw new Error('Неполный отчёт о качестве');
    packet = data;
    receivedAt = Date.now();
    window.dispatchEvent(new CustomEvent('footpredict:quality-loaded', {detail: data}));
    return data;
  })().finally(() => { pending = null; });
  return pending;
}
