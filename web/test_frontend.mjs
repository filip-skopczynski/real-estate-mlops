// Run with Node; optionally pass a generated deals.json to verify its actual schema.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
assert.equal((source.match(/^  load\(\);$/gm) || []).length, 1);
const context = vm.createContext({
  Intl, URL, Date, console,
  document: { getElementById: () => ({ addEventListener() {} }) },
});
vm.runInContext(source.replace(/^  load\(\);$/m, '  globalThis.frontendChecks = { normalizeOffer, safeUrl };'), context);
const { normalizeOffer, safeUrl } = context.frontendChecks;
const fixture = {
  source: 'www.otodom.pl', listing_id: '00123',
  url: 'https://www.otodom.pl/pl/oferta/example-ID123',
  district: 'Mokotów', price_pln: 750000, predicted_price_pln: 1000000,
  area_m2: 50, rooms: 2, observed_at: '2026-10-19T06:00:00+00:00',
};
assert.equal(normalizeOffer(fixture).source, 'otodom');
assert.equal(normalizeOffer(fixture).listing_id, '00123');
assert.equal(normalizeOffer({ ...fixture, source: 'www.olx.pl', url: 'https://www.olx.pl/d/oferta/example-ID123.html' }).source, 'olx');
assert.equal(normalizeOffer({ ...fixture, model_gap_fraction: 99 }).model_gap_fraction, .25);
assert.equal(normalizeOffer({ ...fixture, observed_at: '2026-10-19T06:00:00' }), null);
assert.equal(normalizeOffer({ ...fixture, price_pln: -1 }), null);
assert.equal(normalizeOffer({ ...fixture, source: 'untrusted.example' }), null);
for (const url of [
  'http://www.otodom.pl/pl/oferta/example',
  'https://www.otodom.pl.evil.example/pl/oferta/example',
  'https://user:password@www.otodom.pl/pl/oferta/example',
  'https://www.otodom.pl:999/pl/oferta/example',
  'https://www.otodom.pl/pl/oferta/example?redirect=evil',
  'https://www.otodom.pl/pl/oferta/example#fragment',
  'https://www.otodom.pl/account',
  'javascript:alert(1)',
]) assert.equal(safeUrl(url, 'otodom'), null, url);

const actualPath = process.argv[2];
if (actualPath) {
  const payload = JSON.parse(fs.readFileSync(actualPath, 'utf8'));
  assert.ok(Array.isArray(payload.offers));
  const normalized = payload.offers.map(normalizeOffer).filter(Boolean);
  assert.equal(normalized.length, payload.offers.length, 'Some actual feed rows were rejected');
  assert.ok(normalized.length > 0, 'Actual feed is empty');
  console.log(`Verified all ${normalized.length} actual feed rows, including hosted source names.`);
}
console.log('Frontend source normalization, numeric/date checks, price-gap calculation and URL guards passed.');
