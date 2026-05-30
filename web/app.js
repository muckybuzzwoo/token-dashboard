// app.js — router, state, fetch helpers
import { disposeMountedCharts } from '/web/charts.js';

export const $  = (sel, root=document) => root.querySelector(sel);
export const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

const COMPACT = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
export const fmt = {
  int:   n => (n ?? 0).toLocaleString(),
  compact: n => COMPACT.format(n ?? 0),
  usd:   n => n == null ? '—' : '$' + Number(n).toFixed(2),
  usd4:  n => n == null ? '—' : '$' + Number(n).toFixed(4),
  pct:   n => n == null ? '—' : (n * 100).toFixed(0) + '%',
  short: (s, n=80) => s == null ? '' : (s.length > n ? s.slice(0, n - 1) + '…' : s),
  htmlSafe: s => (s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  modelClass: m => {
    const s = (m || '').toLowerCase();
    if (s.includes('opus'))   return 'opus';
    if (s.includes('sonnet')) return 'sonnet';
    if (s.includes('haiku'))  return 'haiku';
    return '';
  },
  modelShort: m => (m || '').replace('claude-', ''),
  // Browser-local zone: 'sv' locale renders ISO-like "YYYY-MM-DD HH:MM:SS"; slice keeps date + minutes.
  ts:   t => t ? new Date(t).toLocaleString('sv').slice(0, 16) : '',
  time: t => t ? new Date(t).toLocaleTimeString('sv') : '',
};

const PRIVACY_KEY = 'td.privacy-on';

/**
 * Wire click-to-sort on all <th> in a table.
 * Each <td> must carry a data-val attribute with the raw sort value.
 * @param {HTMLTableElement} table
 * @param {{ col?: number, dir?: 'asc'|'desc' }} opts  default sort column index + direction
 */
export function makeSortable(table, { col: defaultCol = 0, dir: defaultDir = 'asc', onChange } = {}) {
  let sortCol = defaultCol;
  let sortDir = defaultDir;
  const ths = [...table.querySelectorAll('thead th')];
  const tbody = table.querySelector('tbody');

  function applySort() {
    ths.forEach((th, i) => {
      th.classList.toggle('sort-asc',  i === sortCol && sortDir === 'asc');
      th.classList.toggle('sort-desc', i === sortCol && sortDir === 'desc');
    });
    const rows = [...tbody.querySelectorAll('tr')];
    rows.sort((a, b) => {
      const av = a.cells[sortCol]?.dataset.val ?? '';
      const bv = b.cells[sortCol]?.dataset.val ?? '';
      const an = Number(av), bn = Number(bv);
      const numeric = av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn);
      const cmp = numeric ? an - bn : av.localeCompare(bv, undefined, { sensitivity: 'base' });
      return sortDir === 'asc' ? cmp : -cmp;
    });
    rows.forEach(r => tbody.appendChild(r));
  }

  ths.forEach((th, i) => {
    th.classList.add('sortable');
    th.addEventListener('click', () => {
      if (sortCol === i) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      else { sortCol = i; sortDir = 'asc'; }
      applySort();
      if (onChange) onChange(sortCol, sortDir);
    });
  });

  applySort(); // initial sort — no callback, state came from caller
}

export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// ── Client-side response cache ────────────────────────────────────────────────
// Keyed by URL, entries expire after 60s. Cleared when SSE signals new data.
const _cache = new Map();
const _CACHE_TTL = 60_000;

export function cacheGet(key) {
  const e = _cache.get(key);
  if (!e) return null;
  if (Date.now() - e.ts > _CACHE_TTL) { _cache.delete(key); return null; }
  return e.data;
}
export function cacheSet(key, data) { _cache.set(key, { data, ts: Date.now() }); }
export function cacheClear()        { _cache.clear(); }

// ── Refresh / countdown state ─────────────────────────────────────────────────
const SCAN_INTERVAL = 60_000; // must match server _scan_loop interval
let _nextScanAt  = Date.now() + SCAN_INTERVAL;
let _scanning    = false;
let _hasNewData  = false;

function updateRefreshBtn() {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  if (_scanning) {
    btn.textContent = '↻ …';
    btn.className = 'refresh-btn scanning';
    return;
  }
  if (_hasNewData) {
    btn.textContent = '↻ new data';
    btn.className = 'refresh-btn has-data';
    return;
  }
  const secs = Math.max(0, Math.round((_nextScanAt - Date.now()) / 1000));
  btn.textContent = `↻ ${secs}s`;
  btn.className = 'refresh-btn';
}

function setScanning(on) {
  _scanning = on;
  const banner = document.getElementById('refresh-banner');
  if (on) {
    if (banner) banner.classList.remove('hidden');
  } else {
    if (banner) banner.classList.add('hidden');
  }
  updateRefreshBtn();
}

export const state = { plan: 'api', pricing: null };

const ROUTES = {
  '/overview':   () => import('/web/routes/overview.js'),
  '/prompts':    () => import('/web/routes/prompts.js'),
  '/sessions':   () => import('/web/routes/sessions.js'),
  '/projects':   () => import('/web/routes/projects.js'),
  '/workspaces': () => import('/web/routes/workspaces.js'),
  '/subagents':  () => import('/web/routes/subagents.js'),
  '/skills':     () => import('/web/routes/skills.js'),
  '/tips':       () => import('/web/routes/tips.js'),
  '/rtk':        () => import('/web/routes/rtk.js'),
  '/settings':   () => import('/web/routes/settings.js'),
};

function buildTopbar() {
  const wrap = document.createElement('header');
  wrap.className = 'topbar';
  wrap.innerHTML = `
    <div class="brand">Token Dashboard</div>
    <nav>
      ${Object.keys(ROUTES).map(p => `<a href="#${p}" data-route="${p}">${p.slice(1)}</a>`).join('')}
    </nav>
    <div class="spacer"></div>
    <span class="pill blur-sensitive" id="plan-pill">api</span>
    <button id="privacy-toggle" class="pill privacy-toggle" type="button" title="Ctrl/Cmd/Alt+B blurs sensitive text" aria-pressed="false">blur off</button>
    <button id="refresh-btn" class="refresh-btn" title="Manually refresh data">↻ 60s</button>
  `;
  document.body.prepend(wrap);

  // Refresh banner — inserted between topbar and #app
  const banner = document.createElement('div');
  banner.id = 'refresh-banner';
  banner.className = 'refresh-banner hidden';
  banner.innerHTML = '<span class="spin">↻</span><span>Getting latest data…</span>';
  document.body.insertBefore(banner, document.getElementById('app'));
}

function setPrivacyMode(on) {
  document.body.classList.toggle('privacy-on', on);
  localStorage.setItem(PRIVACY_KEY, on ? '1' : '0');
  const btn = document.getElementById('privacy-toggle');
  if (!btn) return;
  btn.textContent = on ? 'blur on' : 'blur off';
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

function setActiveTab(routeKey) {
  $$('header.topbar nav a').forEach(a => a.classList.toggle('active', a.dataset.route === routeKey));
}

// Generation counter prevents a slow fetch from a stale render() call
// from overwriting DOM that a newer render() already populated.
let _renderGen = 0;

async function render() {
  const gen = ++_renderGen;
  const hash = location.hash.replace(/^#/, '') || '/overview';
  const path = hash.split('?')[0];
  let key = path;
  if (path.startsWith('/sessions/')) key = '/sessions';
  setActiveTab(key);
  const loader = ROUTES[key] || ROUTES['/overview'];
  const mod = await loader();
  if (gen !== _renderGen) return; // a newer render() won the race — bail out
  disposeMountedCharts();         // dispose all live ECharts instances before clearing DOM
  $('#app').innerHTML = '';
  try {
    await mod.default($('#app'));
  } catch (e) {
    $('#app').innerHTML = `<div class="card"><h2>Error</h2><pre>${fmt.htmlSafe(String(e.stack || e))}</pre></div>`;
  }
}

async function firstRun() {
  if (localStorage.getItem('td.plan-set')) return;
  const plans = Object.entries(state.pricing.plans);
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h2>Welcome — pick your plan</h2>
      <p>This sets how costs are displayed. Change it later in Settings.</p>
      <select id="firstplan" class="blur-sensitive" style="width:100%">
        ${plans.map(([k,v]) => `<option value="${k}">${v.label}${v.monthly ? ` — $${v.monthly}/mo` : ''}</option>`).join('')}
      </select>
      <div class="actions">
        <div class="spacer"></div>
        <button class="primary" id="firstsave">Continue</button>
      </div>
      <p id="firstmsg" class="muted" style="margin:8px 0 0"></p>
    </div>`;
  document.body.appendChild(overlay);
  await new Promise(res => $('#firstsave', overlay).addEventListener('click', async () => {
    const plan = $('#firstplan', overlay).value;
    const msg = $('#firstmsg', overlay);
    msg.textContent = 'Saving...';
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      msg.textContent = err.error || 'Could not save settings.';
      msg.style.color = 'var(--bad)';
      return;
    }
    localStorage.setItem('td.plan-set', '1');
    overlay.remove();
    res();
  }));
  state.plan = (await api('/api/plan')).plan;
  $('#plan-pill').textContent = state.plan;
}

async function boot() {
  buildTopbar();
  setPrivacyMode(localStorage.getItem(PRIVACY_KEY) === '1');
  document.getElementById('privacy-toggle').addEventListener('click', () => {
    setPrivacyMode(!document.body.classList.contains('privacy-on'));
  });

  // Privacy blur (Ctrl/Cmd/Alt+B). Some OSes intercept Windows+B; the
  // topbar button is the fallback when the browser never receives it.
  window.addEventListener('keydown', e => {
    if (!e.repeat && (e.metaKey || e.ctrlKey || e.altKey) && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      setPrivacyMode(!document.body.classList.contains('privacy-on'));
    }
  });

  const planResp = await api('/api/plan');
  state.plan = planResp.plan;
  state.pricing = planResp.pricing;
  $('#plan-pill').textContent = state.plan;

  await firstRun();

  window.addEventListener('hashchange', render);
  await render();

  // Refresh button:
  //   - If new data is buffered → apply it (re-render current page)
  //   - If scanning → ignore (already in flight)
  //   - Otherwise → trigger a manual server scan
  document.getElementById('refresh-btn').addEventListener('click', async () => {
    if (_scanning) return;
    if (_hasNewData) {
      _hasNewData = false;
      render();
      return;
    }
    setScanning(true);
    _nextScanAt = Date.now() + SCAN_INTERVAL;
    try { await fetch('/api/refresh', { method: 'POST' }); } catch {}
  });

  // Countdown ticker — updates the button label every second.
  // When the countdown hits 0, the server scan is imminent; show scanning state.
  setInterval(() => {
    updateRefreshBtn();
    const secs = Math.max(0, Math.round((_nextScanAt - Date.now()) / 1000));
    if (!_scanning && !_hasNewData && secs === 0) setScanning(true);
  }, 1000);

  // SSE diff stream.
  // On scan: invalidate caches and show "new data" badge — do NOT auto-render.
  // Re-rendering charts on every background scan caused ECharts instance
  // accumulation and crashed the browser. User controls when to apply updates.
  try {
    const es = new EventSource('/api/stream');
    es.onmessage = ev => {
      try {
        const evt = JSON.parse(ev.data);
        if (evt.type === 'scan') {
          _nextScanAt = Date.now() + SCAN_INTERVAL;
          // Only flag new data + invalidate cache when the scan actually
          // ingested something — otherwise this is just a no-op heartbeat
          // confirming the scan finished, and we just clear the banner.
          if (evt.n && evt.n.messages > 0) {
            cacheClear();
            _hasNewData = true;
          }
          setScanning(false); // clears spinner; updateRefreshBtn() picks the right label
        }
      } catch {}
    };
  } catch {}
}

boot();
