import {
  authorized, boundedBytes, FEED_LIMIT, response, sha256, validateFeed,
} from '../hosting/lib/feed.mjs';

// Base64 plus the maximum feed stays below D1's 2 MB row limit.
export const MODEL_LIMIT = 1024 * 1024;
const DAY = 86400000;
const STATE_ID = 1;

function json(body, status = 200, headers = {}) {
  return response(body, status, { 'Cache-Control': 'no-store', ...headers });
}

function database(env) {
  if (!env.DB || typeof env.DB.prepare !== 'function') throw new Error('Storage unavailable');
  return typeof env.DB.withSession === 'function' ? env.DB.withSession('first-primary') : env.DB;
}

function base64(bytes) {
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}

function decodedModel(value) {
  if (typeof value !== 'string' || !value || value.length > 4 * Math.ceil(MODEL_LIMIT / 3)) {
    throw new Error('Invalid stored model');
  }
  const binary = atob(value);
  if (!binary.length || binary.length > MODEL_LIMIT) throw new Error('Invalid stored model');
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

export async function readFeed(env, now = Date.now()) {
  const stored = await database(env).prepare(
    'SELECT feed_json FROM website_state WHERE singleton = ?',
  ).bind(STATE_ID).first();
  if (!stored?.feed_json) return json({ error: 'The first ranking is being prepared' }, 503);
  const data = JSON.parse(stored.feed_json);
  // Validate stored structure at its capture time, then explicitly age the rows.
  // Using today's clock for validation would discard a useful stale-feed notice.
  validateFeed(data, Date.parse(data.generated_at));
  const offers = data.offers.filter(row => {
    const age = now - Date.parse(row.observed_at);
    return age >= 0 && age <= 7 * DAY;
  });
  const source_counts = {};
  for (const row of offers) source_counts[row.source] = (source_counts[row.source] ?? 0) + 1;
  return json({
    ...data,
    offers,
    summary: { ...data.summary, showing: offers.length, source_counts },
    stale: now - Date.parse(data.generated_at) > 36 * 3600000,
    served_at: new Date(now).toISOString(),
  });
}

export async function ingestFeed(request, env) {
  if (!await authorized(request, env.WEBSITE_INGEST_TOKEN)) return json({ error: 'Unauthorized' }, 401);
  let input;
  try {
    const bytes = await boundedBytes(request, FEED_LIMIT);
    input = validateFeed(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)));
  } catch {
    return json({ error: 'Invalid or oversized feed' }, 400);
  }
  const generated = Date.parse(input.generated_at);
  const written = await database(env).prepare(`
    UPDATE website_state
    SET feed_json = ?, feed_generated_ms = ?
    WHERE singleton = ? AND model_sha256 = ?
      AND (feed_generated_ms IS NULL OR feed_generated_ms < ?)
  `).bind(JSON.stringify(input), generated, STATE_ID, input.model_sha256, generated).run();
  if (written.meta?.changes !== 1) {
    return json({ error: 'Feed must match the approved model and have a newer capture' }, 409);
  }
  return json({ ok: true, generated_at: input.generated_at, offers: input.offers.length, model_sha256: input.model_sha256 });
}

export async function readModel(request, env) {
  if (!await authorized(request, env.WEBSITE_INGEST_TOKEN)) return json({ error: 'Unauthorized' }, 401);
  const stored = await database(env).prepare(
    'SELECT model_base64, model_sha256 FROM website_state WHERE singleton = ?',
  ).bind(STATE_ID).first();
  if (!stored?.model_base64 || !stored.model_sha256) return json({ error: 'Approved model unavailable' }, 404);
  const bytes = decodedModel(stored.model_base64);
  if (await sha256(bytes) !== stored.model_sha256) throw new Error('Stored model integrity failure');
  return new Response(bytes, {
    headers: {
      'Content-Type': 'application/octet-stream',
      'Cache-Control': 'private, no-store',
      'X-Model-SHA256': stored.model_sha256,
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer',
    },
  });
}

export async function ingestModel(request, env) {
  if (!await authorized(request, env.WEBSITE_INGEST_TOKEN)) return json({ error: 'Unauthorized' }, 401);
  let bytes;
  try {
    bytes = await boundedBytes(request, MODEL_LIMIT);
  } catch {
    return json({ error: 'Invalid or oversized model' }, 400);
  }
  const digest = await sha256(bytes);
  if (request.headers.get('X-Model-SHA256') !== digest) return json({ error: 'Model integrity check failed' }, 400);
  // The same model preserves its feed; changing models invalidates it atomically.
  await database(env).prepare(`
    INSERT INTO website_state (singleton, model_base64, model_sha256, feed_json, feed_generated_ms)
    VALUES (?, ?, ?, NULL, NULL)
    ON CONFLICT(singleton) DO UPDATE SET
      model_base64 = excluded.model_base64,
      model_sha256 = excluded.model_sha256,
      feed_json = CASE WHEN website_state.model_sha256 = excluded.model_sha256
        THEN website_state.feed_json ELSE NULL END,
      feed_generated_ms = CASE WHEN website_state.model_sha256 = excluded.model_sha256
        THEN website_state.feed_generated_ms ELSE NULL END
  `).bind(STATE_ID, base64(bytes), digest).run();
  return json({ ok: true, model_sha256: digest });
}

const routes = new Map([
  ['/api/deals', { method: 'GET', run: (_request, env) => readFeed(env) }],
  ['/api/model', { method: 'GET', run: readModel }],
  ['/api/ingest/deals', { method: 'POST', run: ingestFeed }],
  ['/api/ingest/model', { method: 'PUT', run: ingestModel }],
]);

export async function fetchHandler(request, env) {
  const path = new URL(request.url).pathname;
  try {
    const route = routes.get(path);
    if (route) {
      if (request.method !== route.method) return json({ error: 'Method not allowed' }, 405, { Allow: route.method });
      return await route.run(request, env);
    }
    if (path === '/api' || path.startsWith('/api/')) return json({ error: 'Not found' }, 404);
    if (!['GET', 'HEAD'].includes(request.method)) return json({ error: 'Method not allowed' }, 405, { Allow: 'GET, HEAD' });
    if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') return json({ error: 'Website temporarily unavailable' }, 503);
    return await env.ASSETS.fetch(request);
  } catch {
    return json({ error: 'Website temporarily unavailable' }, 503);
  }
}

export default { fetch: fetchHandler };
