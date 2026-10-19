export const FEED_KEY = 'website/deals.json';
export const MODEL_KEY = 'website/model.joblib';
export const MODEL_LIMIT = 10 * 1024 * 1024;
export const FEED_LIMIT = 512 * 1024;
const DAY = 86400000;
const SOURCES = new Set(['www.olx.pl', 'www.otodom.pl']);

export function response(body, status = 200, extra = {}) {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'no-referrer', ...extra },
  });
}

export async function authorized(request, token) {
  if (typeof token !== 'string' || token.length < 32) return false;
  const supplied = request.headers.get('Authorization') ?? '';
  const digest = async (s) => new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)));
  const [a,b] = await Promise.all([digest(supplied), digest('Bearer ' + token)]);
  let difference = 0;
  for (let i=0;i<a.length;i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export async function boundedBytes(request, limit) {
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) throw new Error('Payload too large');
  const reader = request.body?.getReader();
  if (!reader) throw new Error('Missing payload');
  const chunks = []; let total=0;
  try {
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) { await reader.cancel(); throw new Error('Payload too large'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  if (!total) throw new Error('Missing payload');
  const bytes = new Uint8Array(total); let offset=0;
  for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.byteLength; }
  return bytes;
}

export async function sha256(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
}

function stamp(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error('Invalid timestamp');
  const time=Date.parse(value); if (!Number.isFinite(time)) throw new Error('Invalid timestamp');
  return time;
}
function positive(value) { return typeof value === 'number' && Number.isFinite(value) && value > 0; }
function count(value) { return Number.isSafeInteger(value) && value >= 0; }

export function validateFeed(input, now=Date.now()) {
  if (!input || input.schema_version !== 1 || input.experimental !== true || !Array.isArray(input.offers) || input.offers.length > 50) throw new Error('Invalid feed');
  const generated = stamp(input.generated_at);
  if (generated > now+300000 || generated < now-2*DAY || input.capture_time !== input.generated_at || stamp(input.model_trained_at) > generated || stamp(input.model_capture_time) > stamp(input.model_trained_at)) throw new Error('Invalid capture time');
  if (!/^[a-f0-9]{64}$/.test(input.model_sha256)) throw new Error('Invalid model hash');
  if (input.scope?.city !== 'Warszawa' || input.scope?.transaction !== 'sale' || input.scope?.max_age_days !== 7 || input.scope?.threshold_fraction !== .15) throw new Error('Invalid scope');
  if (!Array.isArray(input.scope.sources) || input.scope.sources.length!==2 || new Set(input.scope.sources).size!==2 || !input.scope.sources.every(source=>SOURCES.has(source))) throw new Error('Invalid sources');
  for (const key of ['catalog_rows','scored_rows','candidates','showing']) if (!count(input.summary?.[key])) throw new Error('Invalid summary');
  if (input.summary.showing !== input.offers.length || input.summary.candidates < input.offers.length || input.summary.candidates > input.summary.scored_rows || input.summary.scored_rows > input.summary.catalog_rows) throw new Error('Conflicting summary');
  const seen=new Set(); let previous=Infinity;
  for (const row of input.offers) {
    if (!SOURCES.has(row.source) || typeof row.listing_id !== 'string' || !/^[0-9]{1,200}$/.test(row.listing_id) || !/[1-9]/.test(row.listing_id)) throw new Error('Invalid listing identity');
    const identity=row.source+':'+row.listing_id;
    if (seen.has(identity)) throw new Error('Duplicate listing'); seen.add(identity);
    if (typeof row.url !== 'string' || /[\x00-\x20\x7f-\x9f\\]/.test(row.url)) throw new Error('Invalid listing URL');
    const url=new URL(row.url);
    const pattern=row.source==='www.olx.pl'? /^\/d\/oferta\/[A-Za-z0-9_-]+\.html$/ : /^\/pl\/oferta\/[A-Za-z0-9_-]+$/;
    if (url.origin!=='https://'+row.source || url.username || url.password || url.search || url.hash || /[?#]/.test(row.url) || !pattern.test(url.pathname)) throw new Error('Invalid listing URL');
    if (!positive(row.price_pln) || !positive(row.predicted_price_pln) || !positive(row.area_m2) || !Number.isSafeInteger(row.rooms) || row.rooms<1 || row.rooms>20) throw new Error('Invalid listing values');
    if (row.district !== null && (typeof row.district!=='string' || row.district.length>120 || /[\x00-\x1f\x7f]/.test(row.district))) throw new Error('Invalid district');
    const observed=stamp(row.observed_at);
    if (observed>generated || observed<generated-7*DAY) throw new Error('Invalid observation time');
    const absolute=row.predicted_price_pln-row.price_pln, gap=absolute/row.predicted_price_pln;
    if (!Number.isFinite(row.model_gap_fraction) || gap < .15 || Math.abs(gap-row.model_gap_fraction)>1e-9 || !Number.isFinite(row.model_gap_pln) || Math.abs(absolute-row.model_gap_pln)>.011 || gap>previous+1e-9) throw new Error('Invalid model gap');
    previous=gap;
  }
  const counts={};
  for (const row of input.offers) counts[row.source]=(counts[row.source]??0)+1;
  if (!input.summary.source_counts || Object.entries(input.summary.source_counts).some(([source,value])=>!SOURCES.has(source)||!count(value)||value!==(counts[source]??0)) || Object.entries(counts).some(([source,value])=>input.summary.source_counts[source]!==value)) throw new Error('Invalid source counts');
  return input;
}

export async function ingestFeed(request, env) {
  if (!await authorized(request,env.WEBSITE_INGEST_TOKEN)) return response({error:'Unauthorized'},401);
  if (!env.BUCKET) return response({error:'Storage unavailable'},503);
  let input;
  try { input=validateFeed(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(await boundedBytes(request,FEED_LIMIT)))); }
  catch { return response({error:'Invalid or oversized feed'},400); }
  const model=await env.BUCKET.head(MODEL_KEY);
  if (!model || model.customMetadata?.sha256 !== input.model_sha256) return response({error:'Feed does not match the approved model'},409);
  const old=await env.BUCKET.get(FEED_KEY);
  if (old) {
    const prior=await old.json();
    if (Date.parse(prior.generated_at)>=Date.parse(input.generated_at)) return response({error:'A newer or equal feed is already stored'},409);
  }
  const written=await env.BUCKET.put(FEED_KEY,JSON.stringify(input), {
    httpMetadata:{contentType:'application/json'},
    ...(old?{onlyIf:{etagMatches:old.etag}}:{onlyIf:{etagDoesNotMatch:'*'}}),
  });
  if (!written) return response({error:'Concurrent update; retry with a fresh capture'},409);
  return response({ok:true,generated_at:input.generated_at,offers:input.offers.length,model_sha256:input.model_sha256});
}

export async function readFeed(env,now=Date.now()) {
  if (!env.BUCKET) return response({error:'Storage unavailable'},503);
  const stored=await env.BUCKET.get(FEED_KEY);
  if (!stored) return response({error:'The first ranking is being prepared'},503);
  const data=await stored.json();
  const offers=data.offers.filter(row=>{const age=now-Date.parse(row.observed_at);return age>=0 && age<=7*DAY;});
  const source_counts={};
  for (const row of offers) source_counts[row.source]=(source_counts[row.source]??0)+1;
  return response({...data,offers,summary:{...data.summary,showing:offers.length,source_counts},stale:now-Date.parse(data.generated_at)>36*3600000,served_at:new Date(now).toISOString()});
}

export async function readModel(request,env) {
  if (!await authorized(request,env.WEBSITE_INGEST_TOKEN)) return response({error:'Unauthorized'},401);
  const model=await env.BUCKET?.get(MODEL_KEY);
  if (!model) return response({error:'Approved model unavailable'},404);
  return new Response(model.body,{headers:{'Content-Type':'application/octet-stream','Cache-Control':'private, no-store','X-Model-SHA256':model.customMetadata.sha256,'X-Content-Type-Options':'nosniff'}});
}

export async function ingestModel(request,env) {
  if (!await authorized(request,env.WEBSITE_INGEST_TOKEN)) return response({error:'Unauthorized'},401);
  if (!env.BUCKET) return response({error:'Storage unavailable'},503);
  let bytes; try { bytes=await boundedBytes(request,MODEL_LIMIT); } catch { return response({error:'Invalid or oversized model'},400); }
  const digest=await sha256(bytes);
  if (request.headers.get('X-Model-SHA256')!==digest) return response({error:'Model integrity check failed'},400);
  await env.BUCKET.put(MODEL_KEY,bytes,{httpMetadata:{contentType:'application/octet-stream'},customMetadata:{sha256:digest}});
  return response({ok:true,model_sha256:digest});
}
