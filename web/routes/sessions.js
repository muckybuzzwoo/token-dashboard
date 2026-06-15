import { api, fmt, makeSortable, cacheGet, cacheSet } from '/web/app.js';

export default async function (root) {
  // Parse /sessions/<id>?filter=...&sort=...&dir=... without triggering re-render
  const hash = location.hash.replace(/^#/, '');
  const [pathPart, qsPart] = hash.split('?');
  const id = decodeURIComponent(pathPart.split('/')[2] || '');
  if (!id) return renderList(root, new URLSearchParams(qsPart || ''));
  return renderSession(root, id, new URLSearchParams(qsPart || ''));
}

// ── Session list ──────────────────────────────────────────────────────────────

async function renderList(root, qs) {
  const url = '/api/sessions?limit=500';
  const cached = cacheGet(url);
  if (cached) { buildList(root, cached, qs); return; }

  const fresh = await api(url);
  cacheSet(url, fresh);
  buildList(root, fresh, qs);
}

const PERIODS = [
  { key: 'all',   label: 'All',     days: null },
  { key: 'today', label: 'Today',   days: 0 },
  { key: '7d',    label: '7 days',  days: 7 },
  { key: '30d',   label: '30 days', days: 30 },
  { key: '90d',   label: '90 days', days: 90 },
];

// Coalesce rapid input events so filtering 500+ rows doesn't thrash the DOM
// on every keystroke. ~150ms feels instant but skips the intermediate work.
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Lightweight type-ahead for an <input>. The native <datalist> popup can't be
// styled or bounded — with long values it renders very wide and unbounded — so
// we own the menu: it opens downward, matches the input width (long values
// ellipsize instead of widening the page), and caps the result count.
function attachAutocomplete(input, items, limit = 8) {
  const wrap = input.parentElement;            // the position:relative .ac span
  const menu = document.createElement('div');
  menu.className = 'ac-menu';
  menu.hidden = true;
  wrap.appendChild(menu);
  let current = [], active = -1, suppress = false;

  function build() {
    if (suppress) { suppress = false; return; }
    const q = input.value.trim().toLowerCase();
    const all = q ? items.filter(s => s.toLowerCase().includes(q)) : items;
    current = all.slice(0, limit);
    active = -1;
    if (!current.length) { menu.hidden = true; return; }
    menu.innerHTML = current.map((s, i) =>
      `<div class="ac-item" data-i="${i}" title="${fmt.htmlSafe(s)}">${fmt.htmlSafe(s)}</div>`).join('')
      + (all.length > current.length
          ? `<div class="ac-more">+${all.length - current.length} more — keep typing…</div>` : '');
    menu.hidden = false;
  }
  function highlight() {
    menu.querySelectorAll('.ac-item').forEach((el, i) => el.classList.toggle('active', i === active));
  }
  function pick(i) {
    if (i < 0 || i >= current.length) return;
    suppress = true;                            // don't re-open the menu for the synthetic input
    input.value = current[i];
    menu.hidden = true;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  input.addEventListener('input', build);
  input.addEventListener('focus', build);
  input.addEventListener('keydown', e => {
    if (menu.hidden) return;
    if (e.key === 'ArrowDown')      { e.preventDefault(); active = Math.min(active + 1, current.length - 1); highlight(); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
    else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); pick(active); }
    else if (e.key === 'Escape')    { menu.hidden = true; }
  });
  // mousedown (not click) fires before the input's blur, so the pick registers.
  menu.addEventListener('mousedown', e => {
    const item = e.target.closest('.ac-item');
    if (item) { e.preventDefault(); pick(Number(item.dataset.i)); }
  });
  input.addEventListener('blur', () => { setTimeout(() => { menu.hidden = true; }, 120); });
}

function buildList(root, list, qs) {
  const initCol = parseInt(qs.get('sort') ?? '0', 10);
  const initDir = qs.get('dir') || 'desc';

  // ── Initial filter state from URL ────────────────────────────────────────
  // A custom from/to range takes precedence over the quick-period tabs.
  const state = {
    project:   qs.get('project') || '',
    q:         qs.get('q') || '',
    period:    PERIODS.some(p => p.key === qs.get('period')) ? qs.get('period') : 'all',
    from:      qs.get('from') || '',
    to:        qs.get('to') || '',
    minCost:   qs.get('mincost') || '',
    minTokens: qs.get('mintokens') || '',
  };
  const customActive = () => !!(state.from || state.to);

  // Distinct project names for the autocomplete (sorted, case-insensitive).
  const projects = [...new Set(list.map(s => s.project_name || s.project_slug).filter(Boolean))]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const projectSet = new Set(projects.map(p => p.toLowerCase()));

  root.innerHTML = `
    <div class="card">
      <div class="flex" style="margin-bottom:14px;flex-wrap:wrap;gap:10px;align-items:center">
        <h2 style="margin:0">Sessions</h2>
        <span class="spacer"></span>
        <div class="range-tabs" id="period-tabs" role="group">
          ${PERIODS.map(p => `<button data-period="${p.key}" class="${!customActive() && p.key === state.period ? 'active' : ''}">${p.label}</button>`).join('')}
        </div>
      </div>
      <div class="flex" style="margin-bottom:10px;flex-wrap:wrap;gap:10px;align-items:center">
        <span class="ac" style="min-width:170px"><input id="f-project" autocomplete="off" placeholder="all projects…" value="${fmt.htmlSafe(state.project)}" style="width:100%" title="Filter by project — type to autocomplete, or pick from the list"></span>
        <span class="ac" style="flex:1;min-width:180px"><input id="f-search" type="search" autocomplete="off" placeholder="search project or session…" value="${fmt.htmlSafe(state.q)}" style="width:100%" title="Substring match on project name or session id"></span>
        <input id="f-mincost" type="number" min="0" step="0.5" placeholder="min $" value="${fmt.htmlSafe(state.minCost)}" style="width:90px" title="Minimum cost (USD)">
        <input id="f-mintokens" type="number" min="0" step="1000" placeholder="min tokens" value="${fmt.htmlSafe(state.minTokens)}" style="width:110px" title="Minimum tokens">
        <button id="f-clear" type="button" title="Clear all filters">Clear</button>
      </div>
      <div class="flex" style="margin-bottom:14px;flex-wrap:wrap;gap:8px;align-items:center">
        <span class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">custom range:</span>
        <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px">from
          <input id="f-from" type="datetime-local" value="${fmt.htmlSafe(state.from)}" title="Start date-time (inclusive)">
        </label>
        <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:6px">to
          <input id="f-to" type="datetime-local" value="${fmt.htmlSafe(state.to)}" title="End date-time (inclusive)">
        </label>
      </div>
      <table id="sessions-table">
        <thead><tr><th>started</th><th>project</th><th class="num">turns</th><th class="num">tokens</th><th class="num">cost</th><th>session</th></tr></thead>
        <tbody>
          ${list.map(s => {
            const proj = s.project_name || s.project_slug || '';
            return `
            <tr data-project="${fmt.htmlSafe(proj)}" data-started="${s.started || ''}" data-turns="${s.turns || 0}" data-tokens="${s.tokens || 0}" data-cost="${s.cost_usd ?? ''}" data-cost-est="${s.cost_estimated ? '1' : ''}" data-session="${fmt.htmlSafe(s.session_id || '')}">
              <td class="mono" data-val="${s.started || ''}">${fmt.ts(s.started)}</td>
              <td data-val="${fmt.htmlSafe(proj)}" title="${fmt.htmlSafe(s.project_slug)}">${fmt.htmlSafe(proj)}</td>
              <td class="num" data-val="${s.turns || 0}">${fmt.int(s.turns)}</td>
              <td class="num" data-val="${s.tokens || 0}">${fmt.int(s.tokens)}</td>
              <td class="num" data-val="${s.cost_usd ?? ''}">${s.cost_usd == null ? '<span class="muted">—</span>' : fmt.usd(s.cost_usd)}${s.cost_estimated ? '<span class="muted" title="pricing estimated from model tier">*</span>' : ''}</td>
              <td data-val="${s.session_id || ''}"><a href="#/sessions/${encodeURIComponent(s.session_id)}" class="mono">${fmt.htmlSafe(s.session_id.slice(0,8))}…</a></td>
            </tr>`;
          }).join('')}
        </tbody>
        <tfoot>
          <tr id="totals-row" style="border-top:2px solid var(--border-2);font-weight:600">
            <td>Totals</td>
            <td class="muted" id="t-count" style="font-weight:400"></td>
            <td class="num" id="t-turns"></td>
            <td class="num" id="t-tokens"></td>
            <td class="num" id="t-cost"></td>
            <td></td>
          </tr>
        </tfoot>
      </table>
    </div>`;

  const tbody = root.querySelector('#sessions-table tbody');
  const rows  = [...tbody.querySelectorAll('tr')];
  const el = sel => root.querySelector(sel);

  // ── URL persistence ──────────────────────────────────────────────────────
  function writeState(col, dir) {
    const p = new URLSearchParams();
    if (state.project)   p.set('project', state.project);
    if (state.q)         p.set('q', state.q);
    if (state.from)      p.set('from', state.from);
    if (state.to)        p.set('to', state.to);
    if (!customActive() && state.period !== 'all') p.set('period', state.period);
    if (state.minCost)   p.set('mincost', state.minCost);
    if (state.minTokens) p.set('mintokens', state.minTokens);
    if (col !== 0 || dir !== 'desc') { p.set('sort', col); p.set('dir', dir); }
    const qstr = p.toString();
    history.replaceState(null, '', '#/sessions' + (qstr ? '?' + qstr : ''));
  }
  let lastCol = initCol, lastDir = initDir;

  // ── Filtering ────────────────────────────────────────────────────────────
  // Returns [minMs, maxMs] absolute bounds; ±Infinity means open-ended.
  function dateBounds() {
    if (customActive()) {
      return [
        state.from ? new Date(state.from).getTime() : -Infinity,
        state.to   ? new Date(state.to).getTime()   :  Infinity,
      ];
    }
    if (state.period === 'all') return [-Infinity, Infinity];
    if (state.period === 'today') { const d = new Date(); d.setHours(0, 0, 0, 0); return [d.getTime(), Infinity]; }
    const days = state.period === '7d' ? 7 : state.period === '30d' ? 30 : 90;
    return [Date.now() - days * 86400000, Infinity];
  }

  function applyFilters() {
    const [minMs, maxMs] = dateBounds();
    const needle  = state.q.trim().toLowerCase();
    const minCost = state.minCost !== '' ? Number(state.minCost) : null;
    const minTok  = state.minTokens !== '' ? Number(state.minTokens) : null;
    // Exact when the value matches a known project (picked from the list);
    // forgiving substring match while the user is still typing.
    const pf      = state.project.trim().toLowerCase();
    const pfExact = pf !== '' && projectSet.has(pf);

    let visible = 0, sumTurns = 0, sumTokens = 0, sumCost = 0, anyEst = false, anyCost = false;

    for (const tr of rows) {
      const proj    = tr.dataset.project || '';
      const started = tr.dataset.started || '';
      const tokens  = Number(tr.dataset.tokens || 0);
      const turns   = Number(tr.dataset.turns || 0);
      const cost    = tr.dataset.cost === '' ? null : Number(tr.dataset.cost);
      const session = tr.dataset.session || '';

      const t = started ? new Date(started).getTime() : NaN;
      const okProject = !pf || (pfExact ? proj.toLowerCase() === pf : proj.toLowerCase().includes(pf));
      const okSearch  = !needle || proj.toLowerCase().includes(needle) || session.toLowerCase().includes(needle);
      const okPeriod  = (minMs === -Infinity && maxMs === Infinity) || (!Number.isNaN(t) && t >= minMs && t <= maxMs);
      const okCost    = minCost == null || (cost != null && cost >= minCost);
      const okTokens  = minTok == null || tokens >= minTok;

      const show = okProject && okSearch && okPeriod && okCost && okTokens;
      tr.style.display = show ? '' : 'none';
      if (show) {
        visible++;
        sumTurns  += turns;
        sumTokens += tokens;
        if (cost != null) { sumCost += cost; anyCost = true; if (tr.dataset.costEst) anyEst = true; }
      }
    }

    el('#t-count').textContent  = `${visible} of ${rows.length} sessions`;
    el('#t-turns').textContent  = fmt.int(sumTurns);
    el('#t-tokens').textContent = fmt.int(sumTokens);
    el('#t-cost').innerHTML     = (anyCost ? fmt.usd(sumCost) : '<span class="muted">—</span>')
      + (anyEst ? '<span class="muted" title="includes pricing estimated from model tier">*</span>' : '');
  }

  // ── Wire up controls ─────────────────────────────────────────────────────
  // Text/number inputs run through a debounce so typing stays smooth even with
  // hundreds of rows; selecting a datalist suggestion also fires 'input'.
  const refresh = debounce(() => { applyFilters(); writeState(lastCol, lastDir); }, 150);
  el('#f-project').addEventListener('input', e => { state.project = e.target.value; refresh(); });
  el('#f-search').addEventListener('input', e => { state.q = e.target.value; refresh(); });
  el('#f-mincost').addEventListener('input', e => { state.minCost = e.target.value; refresh(); });
  el('#f-mintokens').addEventListener('input', e => { state.minTokens = e.target.value; refresh(); });
  attachAutocomplete(el('#f-project'), projects);
  attachAutocomplete(el('#f-search'), projects);

  // Quick-period tabs clear any custom range.
  root.querySelectorAll('#period-tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      state.period = btn.dataset.period;
      state.from = ''; state.to = '';
      el('#f-from').value = ''; el('#f-to').value = '';
      syncPeriodTabs();
      applyFilters(); writeState(lastCol, lastDir);
    });
  });

  // Custom range overrides the quick tabs (deactivates them visually).
  function onCustom() {
    state.from = el('#f-from').value;
    state.to   = el('#f-to').value;
    syncPeriodTabs();
    applyFilters(); writeState(lastCol, lastDir);
  }
  el('#f-from').addEventListener('change', onCustom);
  el('#f-to').addEventListener('change', onCustom);

  function syncPeriodTabs() {
    const custom = customActive();
    root.querySelectorAll('#period-tabs button').forEach(b =>
      b.classList.toggle('active', !custom && b.dataset.period === state.period));
  }

  el('#f-clear').addEventListener('click', () => {
    Object.assign(state, { project: '', q: '', period: 'all', from: '', to: '', minCost: '', minTokens: '' });
    el('#f-project').value = '';
    el('#f-search').value = '';
    el('#f-mincost').value = '';
    el('#f-mintokens').value = '';
    el('#f-from').value = '';
    el('#f-to').value = '';
    syncPeriodTabs();
    applyFilters(); writeState(lastCol, lastDir);
  });

  applyFilters(); // restore on load

  makeSortable(root.querySelector('#sessions-table'), {
    col: initCol,
    dir: initDir,
    onChange: (col, dir) => {
      lastCol = col; lastDir = dir;
      writeState(col, dir);
    },
  });
}

// ── Session detail ────────────────────────────────────────────────────────────

async function renderSession(root, id, qs) {
  const initFilter = qs.get('filter') || 'all';
  const initCol    = parseInt(qs.get('sort') ?? '0', 10);
  const initDir    = qs.get('dir') || 'desc';

  const url = '/api/sessions/' + encodeURIComponent(id);
  const cached = cacheGet(url);
  if (cached) { buildSession(root, id, cached, initFilter, initCol, initDir); return; }

  const turns = await api(url);
  cacheSet(url, turns);
  buildSession(root, id, turns, initFilter, initCol, initDir);
}

function buildSession(root, id, turns, initFilter, initCol, initDir) {
  let totalIn = 0, totalOut = 0, totalCacheRd = 0;
  for (const t of turns) {
    if (t.type !== 'assistant') continue;
    totalIn    += t.input_tokens       || 0;
    totalOut   += t.output_tokens      || 0;
    totalCacheRd += t.cache_read_tokens || 0;
  }
  const slug    = (turns[0] && turns[0].project_slug) || '';
  const cwd     = (turns.find(t => t.cwd) || {}).cwd || '';
  const base    = cwd ? cwd.replace(/\\/g, '/').replace(/\/+$/, '').split('/').pop() : '';
  const project = base || slug;
  const started = (turns[0] && turns[0].timestamp) || '';
  const ended   = (turns[turns.length - 1] && turns[turns.length - 1].timestamp) || '';

  const withTokens = turns.filter(t => (t.input_tokens || 0) + (t.output_tokens || 0) + (t.cache_read_tokens || 0) > 0).length;
  const withIn     = turns.filter(t => (t.input_tokens || 0) > 0).length;
  const withOut    = turns.filter(t => (t.output_tokens || 0) > 0).length;
  const withCache  = turns.filter(t => (t.cache_read_tokens || 0) > 0).length;

  const filterDefs = [
    { key: 'all',    label: `All (${turns.length})` },
    { key: 'tokens', label: `Has tokens (${withTokens})` },
    { key: 'in',     label: `Has in (${withIn})` },
    { key: 'out',    label: `Has out (${withOut})` },
    { key: 'cache',  label: `Has cache (${withCache})` },
  ];

  root.innerHTML = `
    <div class="card">
      <h2 style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;word-break:break-all">
        <span>Session ${fmt.htmlSafe(id)}</span>
        <span class="spacer"></span>
        <a href="#/sessions" class="muted" style="white-space:nowrap">← all sessions</a>
      </h2>
      <div class="flex muted" style="font-family:var(--mono);font-size:12px;flex-wrap:wrap;gap:14px">
        <span>${fmt.htmlSafe(project)}</span>
        <span>${fmt.ts(started)} → ${fmt.ts(ended)}</span>
        <span>${turns.length} records</span>
        <span>${fmt.int(totalIn)} in · ${fmt.int(totalOut)} out · ${fmt.int(totalCacheRd)} cache rd</span>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="flex" style="margin-bottom:12px;flex-wrap:wrap;gap:10px">
        <h3 style="margin:0">Turn-by-turn</h3>
        <div class="spacer"></div>
        <div class="flex" style="gap:6px;flex-wrap:wrap;align-items:center">
          <span class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">show:</span>
          <div class="range-tabs" id="turn-filters" role="group">
            ${filterDefs.map(f => `<button data-filter="${f.key}" class="${f.key === initFilter ? 'active' : ''}">${f.label}</button>`).join('')}
          </div>
        </div>
      </div>
      <table id="session-turns-table">
        <thead><tr>
          <th>time</th><th>type</th><th>model</th>
          <th class="blur-sensitive">prompt / tools</th>
          <th class="num">in</th><th class="num">out</th><th class="num">cache rd</th>
        </tr></thead>
        <tbody>
          ${turns.map(t => {
            const tools = t.tool_calls_json ? JSON.parse(t.tool_calls_json) : [];
            const summary = t.prompt_text ? fmt.short(t.prompt_text, 110)
              : tools.length ? tools.map(x => x.name).join(' · ')
              : '';
            const tin  = t.input_tokens       || 0;
            const tout = t.output_tokens      || 0;
            const trd  = t.cache_read_tokens  || 0;
            return `<tr data-tin="${tin}" data-tout="${tout}" data-trd="${trd}">
              <td class="mono" data-val="${t.timestamp || ''}">${fmt.time(t.timestamp)}</td>
              <td data-val="${t.type || ''}">${t.type}${t.is_sidechain ? ' <span class="badge">side</span>' : ''}</td>
              <td data-val="${t.model || ''}">${t.model ? `<span class="badge ${fmt.modelClass(t.model)}">${fmt.htmlSafe(fmt.modelShort(t.model))}</span>` : ''}</td>
              <td class="blur-sensitive" data-val="">${fmt.htmlSafe(summary)}</td>
              <td class="num" data-val="${tin}">${fmt.int(tin)}</td>
              <td class="num" data-val="${tout}">${fmt.int(tout)}</td>
              <td class="num" data-val="${trd}">${fmt.int(trd)}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;

  // ── State & URL persistence ──────────────────────────────────────────────
  const viewState = { filter: initFilter, col: initCol, dir: initDir };

  function writeState() {
    const p = new URLSearchParams();
    if (viewState.filter !== 'all') p.set('filter', viewState.filter);
    if (viewState.col !== 0 || viewState.dir !== 'desc') {
      p.set('sort', viewState.col);
      p.set('dir', viewState.dir);
    }
    const q = p.toString();
    history.replaceState(null, '', '#/sessions/' + encodeURIComponent(id) + (q ? '?' + q : ''));
  }

  // ── Filter ───────────────────────────────────────────────────────────────
  const tbody = root.querySelector('#session-turns-table tbody');

  function applyFilter(filter) {
    tbody.querySelectorAll('tr').forEach(tr => {
      const tin  = Number(tr.dataset.tin  || 0);
      const tout = Number(tr.dataset.tout || 0);
      const trd  = Number(tr.dataset.trd  || 0);
      const show =
        filter === 'all'    ? true :
        filter === 'tokens' ? tin + tout + trd > 0 :
        filter === 'in'     ? tin  > 0 :
        filter === 'out'    ? tout > 0 :
        filter === 'cache'  ? trd  > 0 : true;
      tr.style.display = show ? '' : 'none';
    });
  }

  applyFilter(initFilter); // restore on load

  root.querySelectorAll('#turn-filters button').forEach(btn => {
    btn.addEventListener('click', () => {
      root.querySelectorAll('#turn-filters button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      viewState.filter = btn.dataset.filter;
      applyFilter(viewState.filter);
      writeState();
    });
  });

  // ── Sort ─────────────────────────────────────────────────────────────────
  makeSortable(root.querySelector('#session-turns-table'), {
    col: initCol,
    dir: initDir,
    onChange: (col, dir) => {
      viewState.col = col;
      viewState.dir = dir;
      writeState();
    },
  });
}
