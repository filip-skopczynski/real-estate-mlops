(() => {
  'use strict';
  const state = { offers: [], visible: 9, generatedAt: null };
  const $ = (id) => document.getElementById(id);
  const ids = ['search', 'district', 'source', 'min-price', 'max-price', 'rooms', 'sort'];
  const formatInteger = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
  const formatArea = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 2 });
  const formatPercent = new Intl.NumberFormat('en-GB', { style: 'percent', maximumFractionDigits: 1 });
  const dateFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'Europe/Warsaw' });
  const timeFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Warsaw' });
  const svgPaths = {
    location: '<path d="M15.5 8.2c0 4-5.5 8.3-5.5 8.3S4.5 12.2 4.5 8.2a5.5 5.5 0 0 1 11 0Z"/><circle cx="10" cy="8" r="1.7"/>',
    area: '<path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4M3 3l5 5m9-5-5 5m5 9-5-5M3 17l5-5"/>',
    rooms: '<path d="M3 17V5h14v12M3 12h14M9 5v7M7 17v-3h6v3M1 17h18"/>',
    star: '<path d="m10 2 2.2 5.2 5.6.5-4.3 3.7 1.3 5.5-4.8-2.8-4.8 2.8 1.3-5.5-4.3-3.7 5.6-.5Z"/>'
  };
  const buildingMarkup = '<svg class="building-art" viewBox="0 0 150 165" fill="none" aria-hidden="true"><path d="M0 157h150M15 157V66h46v91M61 157V28h53v129M114 157V91h30v66" fill="#edf3e3"/><path d="M15 157V66h46v91M61 157V28h53v129M114 157V91h30v66M61 28l53-15v15M24 77h10v12H24zm19 0h10v12H43zm-19 25h10v12H24zm19 0h10v12H43zm-19 25h10v12H24zm19 0h10v12H43zM71 41h12v14H71zm21 0h12v14H92zm-21 26h12v14H71zm21 0h12v14H92zm-21 26h12v14H71zm21 0h12v14H92zm-21 26h12v14H71zm21 0h12v14H92zM123 103h13m-13 16h13m-13 16h13M80 157v-14h16v14M5 157v-24m0 12c-10-5-10-17 0-20 10 3 10 15 0 20"/></svg>';
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function icon(name) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    el.setAttribute('viewBox', '0 0 20 20');
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = svgPaths[name] || '';
    return el;
  }
  function number(value) { return typeof value === 'number' && Number.isFinite(value) ? value : null; }
  function date(value) {
    if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return null;
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  function safeUrl(raw, source) {
    if (typeof raw !== 'string' || /[\u0000-\u0020\u007f]/.test(raw)) return null;
    try {
      const url = new URL(raw);
      const host = source === 'olx' ? 'olx.pl' : source === 'otodom' ? 'otodom.pl' : null;
      if (!host || url.protocol !== 'https:' || url.username || url.password || url.port || url.hash || url.search) return null;
      if (url.hostname !== host && url.hostname !== `www.${host}`) return null;
      const prefix = source === 'olx' ? '/d/oferta/' : '/pl/oferta/';
      return url.pathname.startsWith(prefix) && url.pathname.length > prefix.length ? url.href : null;
    } catch { return null; }
  }
  function normalizeOffer(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const rawSource = String(raw.source || '').toLowerCase();
    const source = ({ olx: 'olx', 'olx.pl': 'olx', 'www.olx.pl': 'olx', otodom: 'otodom', 'otodom.pl': 'otodom', 'www.otodom.pl': 'otodom' })[rawSource] || '';
    const url = safeUrl(raw.url, source);
    const price = number(raw.price_pln);
    const predicted = number(raw.predicted_price_pln);
    const area = number(raw.area_m2);
    const rooms = number(raw.rooms);
    const observed = date(raw.observed_at);
    const id = String(raw.listing_id || '');
    if (!url || !price || price <= 0 || !predicted || predicted <= 0 || !area || area <= 0 || !observed || !/^\d+$/.test(id)) return null;
    return { ...raw, source, url, listing_id: id, price_pln: price, predicted_price_pln: predicted, area_m2: area, rooms: rooms && rooms > 0 ? rooms : null, district: typeof raw.district === 'string' && raw.district.trim() ? raw.district.trim() : 'Warsaw', observed, model_gap_fraction: (predicted - price) / predicted, model_gap_pln: predicted - price };
  }
  function sourceLabel(source) { return source === 'olx' ? 'OLX' : 'Otodom'; }
  function money(value) { return formatInteger.format(value); }
  function median(values) {
    const items = [...values].sort((a, b) => a - b);
    const middle = Math.floor(items.length / 2);
    return items.length ? items.length % 2 ? items[middle] : (items[middle - 1] + items[middle]) / 2 : null;
  }
  function link(offer, label, className) {
    const a = node('a', className, label);
    a.href = offer.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.setAttribute('aria-label', `View ${offer.district} apartment on ${sourceLabel(offer.source)} (opens in a new tab)`);
    a.append(node('span', '', '↗'));
    return a;
  }
  function locationLine(offer) {
    const el = node('div', 'offer-location');
    el.append(icon('location'), node('span', '', offer.district === 'Warsaw' ? 'Warsaw · District unspecified' : 'Warsaw · Apartment for sale'));
    return el;
  }
  function propertyDetails(offer) {
    const el = node('div', 'property-details');
    const area = node('span');
    area.append(icon('area'), node('span', '', `${formatArea.format(offer.area_m2)} m²`));
    const rooms = node('span');
    const roomText = offer.rooms === null ? 'Rooms unspecified' : `${formatArea.format(offer.rooms)} ${offer.rooms === 1 ? 'room' : 'rooms'}`;
    rooms.append(icon('rooms'), node('span', '', roomText));
    el.append(area, rooms);
    return el;
  }
  function sourceTag(offer) { return node('span', 'source-tag', sourceLabel(offer.source)); }
  function featured(offer) {
    const article = node('article', 'featured-card');
    const art = node('div', 'featured-illustration');
    art.setAttribute('aria-hidden', 'true');
    art.innerHTML = buildingMarkup;
    art.prepend(node('span', 'featured-rank', 'FIRST IN YOUR RESULTS'));
    const content = node('div', 'featured-content');
    const eyebrow = node('div', 'featured-eyebrow');
    eyebrow.append(icon('star'), node('span', '', 'LEADING MATCH IN YOUR RESULTS'));
    const source = node('div');
    source.append(sourceTag(offer), node('span', 'offer-observed', `Observed ${dateFormat.format(offer.observed)}`));
    content.append(eyebrow, node('h3', 'offer-title', offer.district), locationLine(offer), propertyDetails(offer), source);
    const pricing = node('div', 'featured-pricing');
    pricing.append(node('div', 'gap-label', 'Below model estimate'), node('div', 'gap-value', formatPercent.format(offer.model_gap_fraction)), node('div', 'featured-price', `${money(offer.price_pln)} PLN`), node('div', 'estimate-line', `Model estimate ${money(offer.predicted_price_pln)} PLN`), link(offer, 'View original listing', 'button button-primary'));
    article.append(art, content, pricing);
    return article;
  }
  function card(offer, index) {
    const article = node('article', 'offer-card');
    const top = node('div', 'card-top');
    top.append(node('span', 'rank-number', `MATCH ${String(index + 1).padStart(2, '0')}`), node('span', 'gap-badge', `${formatPercent.format(offer.model_gap_fraction)} model gap`));
    const content = node('div', 'card-content');
    content.append(node('h3', 'offer-title', offer.district), locationLine(offer), propertyDetails(offer));
    const pricing = node('div', 'card-pricing');
    const asking = node('div');
    const amount = node('div', 'asking-price', money(offer.price_pln));
    amount.append(node('span', 'price-currency', 'PLN'));
    asking.append(node('span', 'price-label', 'ASKING PRICE'), amount);
    const model = node('div', 'model-estimate', 'Model estimate');
    model.append(node('strong', '', `${money(offer.predicted_price_pln)} PLN`));
    pricing.append(asking, model);
    content.append(pricing);
    const bottom = node('div', 'card-bottom');
    const source = node('div', 'card-source-group');
    source.append(sourceTag(offer), node('span', 'card-date', dateFormat.format(offer.observed)));
    bottom.append(source, link(offer, 'View listing', 'listing-link'));
    article.append(top, content, bottom);
    return article;
  }
  function values() {
    return { search: $('search').value.trim().toLocaleLowerCase(), district: $('district').value, source: $('source').value, min: $('min-price').value === '' ? null : Number($('min-price').value), max: $('max-price').value === '' ? null : Number($('max-price').value), rooms: $('rooms').value, sort: $('sort').value };
  }
  function filtered() {
    const f = values();
    const offers = state.offers.filter((o) => (!f.search || `${o.district} ${o.source} ${o.listing_id}`.toLocaleLowerCase().includes(f.search)) && (!f.district || o.district === f.district) && (!f.source || o.source === f.source) && (f.min === null || o.price_pln >= f.min) && (f.max === null || o.price_pln <= f.max) && (!f.rooms || o.rooms !== null && (f.rooms === '4' ? o.rooms >= 4 : o.rooms === Number(f.rooms))));
    offers.sort((a, b) => f.sort === 'price-low' ? a.price_pln - b.price_pln : f.sort === 'price-high' ? b.price_pln - a.price_pln : f.sort === 'newest' ? b.observed - a.observed : b.model_gap_fraction - a.model_gap_fraction || b.model_gap_pln - a.model_gap_pln);
    return offers;
  }
  function render() {
    const offers = filtered();
    const f = values();
    const count = $('results-count');
    count.replaceChildren(node('strong', '', offers.length), document.createTextNode(` ${offers.length === 1 ? 'apartment' : 'apartments'} in your results`));
    $('clear-filters').hidden = !(f.search || f.district || f.source || f.min !== null || f.max !== null || f.rooms);
    $('featured-offer').replaceChildren();
    $('offer-list').replaceChildren();
    $('empty-state').hidden = offers.length > 0;
    if (offers.length) {
      $('featured-offer').append(featured(offers[0]));
      const fragment = document.createDocumentFragment();
      offers.slice(1, state.visible + 1).forEach((o, i) => fragment.append(card(o, i + 1)));
      $('offer-list').append(fragment);
    }
    const remaining = Math.max(0, offers.length - 1 - state.visible);
    $('load-more').hidden = remaining === 0;
    if (remaining) $('load-more').replaceChildren(document.createTextNode(`Show ${Math.min(9, remaining)} more apartments`), node('span', '', '↓'));
  }
  function reset() {
    ids.forEach((id) => { $(id).value = id === 'sort' ? 'gap' : ''; });
    state.visible = 9;
    render();
  }
  function setStats() {
    $('stat-count').textContent = formatInteger.format(state.offers.length);
    const price = median(state.offers.map((o) => o.price_pln));
    const gap = median(state.offers.map((o) => o.model_gap_fraction));
    $('stat-price').replaceChildren(document.createTextNode(price === null ? '—' : money(price)), ...(price === null ? [] : [node('small', '', 'PLN')]));
    $('stat-gap').textContent = gap === null ? '—' : formatPercent.format(gap);
  }
  async function load() {
    try {
      const response = await fetch('/api/deals', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!response.ok) throw new Error('Ranking unavailable');
      const data = await response.json();
      if (!data || !Array.isArray(data.offers)) throw new Error('Invalid ranking');
      state.offers = data.offers.map(normalizeOffer).filter(Boolean);
      state.generatedAt = date(data.generated_at);
      const trained = date(data.model_trained_at);
      $('hero-update').textContent = state.generatedAt ? `Ranking updated ${timeFormat.format(state.generatedAt)} · Warsaw time` : 'Saved ranking · Update time unavailable';
      if (trained) $('model-date').textContent = `Model trained ${dateFormat.format(trained)}`;
      const districts = [...new Set(state.offers.map((o) => o.district))].sort((a, b) => a.localeCompare(b, 'pl'));
      districts.forEach((district) => { const option = node('option', '', district); option.value = district; $('district').append(option); });
      const ageDays = state.generatedAt ? (Date.now() - state.generatedAt.getTime()) / 86400000 : null;
      if (ageDays === null || ageDays > 1.5) {
        $('ranking-notice').hidden = false;
        $('ranking-notice').textContent = ageDays === null ? 'This is a saved ranking. Its generation time is unavailable. Open the original listing to check its current price and availability.' : `This saved ranking was generated on ${dateFormat.format(state.generatedAt)}. Prices and availability may have changed; check the original listing.`;
      }
      setStats();
      render();
      if (!state.offers.length) {
        $('empty-state').querySelector('h3').textContent = 'No ranked apartments are available yet';
        $('empty-state').querySelector('p').textContent = 'The next generated ranking will appear here when it is published.';
        $('reset-empty').hidden = true;
      }
    } catch {
      $('featured-offer').replaceChildren();
      $('offer-list').replaceChildren();
      $('empty-state').hidden = false;
      $('empty-state').querySelector('h3').textContent = 'The ranking is unavailable right now';
      $('empty-state').querySelector('p').textContent = 'Please try again in a moment. No offer data has been substituted.';
      $('reset-empty').textContent = 'Try again';
      $('reset-empty').onclick = () => window.location.reload();
      $('results-count').textContent = 'Unable to load apartments';
      $('hero-update').textContent = 'Ranking temporarily unavailable';
      $('filters').querySelectorAll('input, select').forEach((el) => { el.disabled = true; });
      $('sort').disabled = true;
    }
  }
  $('filters').addEventListener('submit', (event) => event.preventDefault());
  ids.forEach((id) => $(id).addEventListener(id === 'search' || id.includes('price') ? 'input' : 'change', () => { state.visible = 9; render(); }));
  $('clear-filters').addEventListener('click', reset);
  $('reset-empty').addEventListener('click', reset);
  $('load-more').addEventListener('click', () => { state.visible += 9; render(); });
  load();
})();
