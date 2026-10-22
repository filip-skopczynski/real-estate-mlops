import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { fetchHandler, ingestFeed, ingestModel, readFeed, readModel, MODEL_LIMIT } from '../worker.mjs';
import { sha256, FEED_LIMIT } from '../../hosting/lib/feed.mjs';

const token = 'owner-only-test-token-'.repeat(3);
const origin = 'https://example.workers.dev';
const migration = readFileSync(new URL('../migrations/0001_state.sql', import.meta.url), 'utf8');
const bytes = new TextEncoder().encode('trusted test model bytes');
const digest = await sha256(bytes);

function storage() {
  const sql = new DatabaseSync(':memory:');
  sql.exec(migration);
  const sessions = [];
  let queries = 0;
  const DB = {
    withSession(value) { sessions.push(value); return this; },
    prepare(query) {
      queries += 1;
      const statement = sql.prepare(query);
      return { bind(...args) {
        return {
          async first() { return statement.get(...args) ?? null; },
          async run() { return { success: true, meta: { changes: Number(statement.run(...args).changes) } }; },
        };
      } };
    },
  };
  return { DB, sql, sessions, queries: () => queries };
}

function fixture(milliseconds = Date.now() - 1000) {
  const stamp = new Date(milliseconds).toISOString();
  return {
    schema_version: 1, experimental: true, generated_at: stamp, capture_time: stamp,
    model_capture_time: '2026-10-15T08:44:00Z', model_trained_at: '2026-10-15T08:45:00Z', model_sha256: digest,
    scope: { city: 'Warszawa', transaction: 'sale', max_age_days: 7, threshold_fraction: .15, sources: ['www.olx.pl', 'www.otodom.pl'] },
    summary: { catalog_rows: 20, scored_rows: 15, candidates: 2, showing: 1, source_counts: { 'www.otodom.pl': 1 } },
    offers: [{ source: 'www.otodom.pl', listing_id: '0123', url: 'https://www.otodom.pl/pl/oferta/apartment-ID123', district: 'Wola', price_pln: 800000, predicted_price_pln: 1000000, model_gap_fraction: .2, model_gap_pln: 200000, area_m2: 50, rooms: 2, observed_at: stamp }],
  };
}

function modelRequest(body = bytes, hash = digest, auth = token) {
  return new Request(origin + '/api/ingest/model', {
    method: 'PUT', body, headers: { Authorization: 'Bearer ' + auth, 'X-Model-SHA256': hash },
  });
}

function feedRequest(payload, auth = token) {
  return new Request(origin + '/api/ingest/deals', {
    method: 'POST', headers: { Authorization: 'Bearer ' + auth, 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}

async function initialized() {
  const store = storage();
  const env = { DB: store.DB, WEBSITE_INGEST_TOKEN: token };
  assert.equal((await ingestModel(modelRequest(), env)).status, 200);
  return { ...store, env };
}

test('migration seeds one empty state and database constraints bound stored payloads', () => {
  const store = storage();
  try {
    assert.equal(store.sql.prepare('SELECT count(*) AS n FROM website_state').get().n, 1);
    assert.throws(() => store.sql.prepare('INSERT INTO website_state (singleton) VALUES (2)').run());
    assert.throws(() => store.sql.prepare('UPDATE website_state SET model_sha256 = ?').run('a'.repeat(64)));
    assert.throws(() => store.sql.prepare('UPDATE website_state SET model_base64 = ?, model_sha256 = ?').run('a'.repeat(1398105), 'a'.repeat(64)));
  } finally { store.sql.close(); }
});

test('privileged routes reject missing, wrong or short tokens before touching storage', async () => {
  for (const supplied of [undefined, 'incorrect']) {
    let touched = false;
    const env = { WEBSITE_INGEST_TOKEN: token, get DB() { touched = true; throw new Error('must not access'); } };
    for (const [path, method, body] of [['/api/model', 'GET'], ['/api/ingest/deals', 'POST', '{}'], ['/api/ingest/model', 'PUT', 'data']]) {
      const request = new Request(origin + path, { method, body, headers: supplied ? { Authorization: 'Bearer ' + supplied } : {} });
      assert.equal((await fetchHandler(request, env)).status, 401);
      assert.equal(touched, false);
    }
  }
  assert.equal((await ingestModel(modelRequest(), { WEBSITE_INGEST_TOKEN: 'short' })).status, 401);
});

test('public dashboard assets and feed need no Sites or ingestion token', async () => {
  const { env, sql, sessions } = await initialized();
  try {
    const payload = fixture();
    assert.equal((await ingestFeed(feedRequest(payload), env)).status, 200);
    const result = await fetchHandler(new Request(origin + '/api/deals'), env);
    const data = await result.json();
    assert.equal(result.status, 200);
    assert.equal(data.offers.length, 1);
    assert.equal(data.offers[0].listing_id, '0123');
    assert.equal(result.headers.get('Cache-Control'), 'no-store');
    assert.equal('model_base64' in data, false);
    assert.ok(sessions.every(value => value === 'first-primary'));
    let received;
    env.ASSETS = { fetch: async request => { received = request.url; return new Response('<html>Dashboard</html>'); } };
    assert.equal((await fetchHandler(new Request(origin + '/'), env)).status, 200);
    assert.equal(received, origin + '/');
  } finally { sql.close(); }
});

test('empty state reports unavailable feed and missing model without inventing offers', async () => {
  const { DB, sql } = storage();
  const env = { DB, WEBSITE_INGEST_TOKEN: token };
  try {
    assert.equal((await readFeed(env)).status, 503);
    assert.equal((await readModel(new Request(origin + '/api/model', { headers: { Authorization: 'Bearer ' + token } }), env)).status, 404);
    assert.equal((await ingestFeed(feedRequest(fixture()), env)).status, 409);
  } finally { sql.close(); }
});

test('model bytes round-trip exactly with their verified digest and remain private', async () => {
  const { env, sql } = await initialized();
  try {
    const result = await readModel(new Request(origin + '/api/model', { headers: { Authorization: 'Bearer ' + token } }), env);
    assert.equal(result.status, 200);
    assert.equal(result.headers.get('X-Model-SHA256'), digest);
    assert.match(result.headers.get('Cache-Control'), /no-store/);
    assert.deepEqual(new Uint8Array(await result.arrayBuffer()), bytes);
    sql.prepare('UPDATE website_state SET model_base64 = ?').run(btoa('tampered'));
    assert.equal((await fetchHandler(new Request(origin + '/api/model', { headers: { Authorization: 'Bearer ' + token } }), env)).status, 503);
  } finally { sql.close(); }
});

test('body limits and hash mismatch fail before database access', async () => {
  const env = { WEBSITE_INGEST_TOKEN: token, get DB() { throw new Error('must not access storage'); } };
  assert.equal((await ingestModel(modelRequest(bytes, '0'.repeat(64)), env)).status, 400);
  assert.equal((await ingestModel(modelRequest(new Uint8Array(MODEL_LIMIT + 1)), env)).status, 400);
  const request = new Request(origin + '/api/ingest/deals', { method: 'POST', body: 'x'.repeat(FEED_LIMIT + 1), headers: { Authorization: 'Bearer ' + token } });
  assert.equal((await ingestFeed(request, env)).status, 400);
  const invalid = fixture();
  invalid.offers[0].url = 'https://hostile.example/';
  assert.equal((await ingestFeed(feedRequest(invalid), env)).status, 400);
});

test('the full 1 MiB model boundary fits storage and round-trips without truncation', async () => {
  const { DB, sql } = storage();
  const env = { DB, WEBSITE_INGEST_TOKEN: token };
  try {
    const body = Uint8Array.from({ length: MODEL_LIMIT }, (_, index) => index % 251);
    const hash = await sha256(body);
    assert.equal((await ingestModel(modelRequest(body, hash), env)).status, 200);
    const state = sql.prepare('SELECT length(model_base64) AS size FROM website_state').get();
    assert.equal(state.size, 1398104);
    assert.ok(state.size + FEED_LIMIT < 2000000);
    const result = await readModel(new Request(origin + '/api/model', { headers: { Authorization: 'Bearer ' + token } }), env);
    assert.deepEqual(new Uint8Array(await result.arrayBuffer()), body);
  } finally { sql.close(); }
});

test('unapproved model hash, equal capture and older capture cannot replace the feed', async () => {
  const { env, sql } = await initialized();
  try {
    const now = Date.now() - 1000;
    const original = fixture(now);
    assert.equal((await ingestFeed(feedRequest(original), env)).status, 200);
    const unapproved = fixture(now + 1);
    unapproved.model_sha256 = 'b'.repeat(64);
    for (const rejected of [original, fixture(now - 1), unapproved]) {
      assert.equal((await ingestFeed(feedRequest(rejected), env)).status, 409);
    }
    assert.equal(sql.prepare('SELECT feed_generated_ms FROM website_state').get().feed_generated_ms, now);
    assert.equal((await ingestFeed(feedRequest(fixture(now + 1)), env)).status, 200);
  } finally { sql.close(); }
});

test('competing publishers cannot leave an older snapshot after a newer capture', async () => {
  const { env, sql } = await initialized();
  try {
    const now = Date.now() - 1000;
    const results = await Promise.all([ingestFeed(feedRequest(fixture(now + 10)), env), ingestFeed(feedRequest(fixture(now)), env)]);
    assert.equal(results[0].status, 200);
    assert.ok(results[1].status === 200 || results[1].status === 409);
    assert.equal(sql.prepare('SELECT feed_generated_ms FROM website_state').get().feed_generated_ms, now + 10);
  } finally { sql.close(); }
});

test('same model preserves feed while changed model clears it atomically', async () => {
  const { env, sql } = await initialized();
  try {
    assert.equal((await ingestFeed(feedRequest(fixture()), env)).status, 200);
    assert.equal((await ingestModel(modelRequest(), env)).status, 200);
    assert.equal((await readFeed(env)).status, 200);
    const newerBytes = new TextEncoder().encode('new owner-approved model');
    const newerHash = await sha256(newerBytes);
    assert.equal((await ingestModel(modelRequest(newerBytes, newerHash), env)).status, 200);
    const state = sql.prepare('SELECT model_sha256, feed_json, feed_generated_ms FROM website_state').get();
    assert.equal(state.model_sha256, newerHash);
    assert.equal(state.feed_json, null);
    assert.equal(state.feed_generated_ms, null);
    assert.equal((await ingestFeed(feedRequest(fixture()), env)).status, 409);
  } finally { sql.close(); }
});

test('reads drop expired or future observations and flag stale capture with accurate counts', async () => {
  const { env, sql } = await initialized();
  try {
    const payload = fixture();
    assert.equal((await ingestFeed(feedRequest(payload), env)).status, 200);
    const capture = Date.parse(payload.generated_at);
    const stale = await (await readFeed(env, capture + 2 * 86400000)).json();
    assert.equal(stale.stale, true);
    assert.equal(stale.offers.length, 1);
    const expired = await (await readFeed(env, capture + 8 * 86400000)).json();
    assert.equal(expired.offers.length, 0);
    assert.equal(expired.summary.showing, 0);
    assert.deepEqual(expired.summary.source_counts, {});
    const future = await (await readFeed(env, capture - 1)).json();
    assert.equal(future.offers.length, 0);
  } finally { sql.close(); }
});

test('unknown API routes never fall through to assets and methods are explicit', async () => {
  const env = { ASSETS: { fetch() { throw new Error('API must not reach assets'); } } };
  for (const path of ['/api', '/api/unknown', '/api/ingest/model/']) {
    assert.equal((await fetchHandler(new Request(origin + path), env)).status, 404);
  }
  for (const [path, method, allow] of [['/api/deals', 'POST', 'GET'], ['/api/model', 'PUT', 'GET'], ['/api/ingest/deals', 'GET', 'POST'], ['/api/ingest/model', 'POST', 'PUT'], ['/', 'POST', 'GET, HEAD']]) {
    const result = await fetchHandler(new Request(origin + path, { method }), env);
    assert.equal(result.status, 405);
    assert.equal(result.headers.get('Allow'), allow);
  }
});

test('database failures return sanitized 503 responses without SQL, credentials or stack traces', async () => {
  const env = { WEBSITE_INGEST_TOKEN: token, DB: { prepare() { throw new Error('secret-password SQL SELECT database.example'); } } };
  for (const request of [new Request(origin + '/api/deals'), new Request(origin + '/api/model', { headers: { Authorization: 'Bearer ' + token } }), modelRequest(), feedRequest(fixture())]) {
    const result = await fetchHandler(request, env);
    assert.equal(result.status, 503);
    const body = await result.text();
    assert.equal(body.includes('secret-password'), false);
    assert.equal(body.includes('database.example'), false);
    assert.equal(body.includes('SELECT'), false);
  }
});
