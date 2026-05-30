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
  const url = '/api/sessions?limit=100';
  const cached = cacheGet(url);
  if (cached) { buildList(root, cached, qs); return; }

  const fresh = await api(url);
  cacheSet(url, fresh);
  buildList(root, fresh, qs);
}

function buildList(root, list, qs) {
  const initCol = parseInt(qs.get('sort') ?? '0', 10);
  const initDir = qs.get('dir') || 'desc';

  root.innerHTML = `
    <div class="card">
      <h2>Sessions</h2>
      <table id="sessions-table">
        <thead><tr><th>started</th><th>project</th><th class="num">turns</th><th class="num">tokens</th><th>session</th></tr></thead>
        <tbody>
          ${list.map(s => `
            <tr>
              <td class="mono" data-val="${s.started || ''}">${fmt.ts(s.started)}</td>
              <td class="blur-sensitive" data-val="${fmt.htmlSafe(s.project_name || s.project_slug)}" title="${fmt.htmlSafe(s.project_slug)}">${fmt.htmlSafe(s.project_name || s.project_slug)}</td>
              <td class="num" data-val="${s.turns || 0}">${fmt.int(s.turns)}</td>
              <td class="num" data-val="${s.tokens || 0}">${fmt.int(s.tokens)}</td>
              <td data-val="${s.session_id || ''}"><a href="#/sessions/${encodeURIComponent(s.session_id)}" class="mono blur-sensitive">${fmt.htmlSafe(s.session_id.slice(0,8))}…</a></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;

  makeSortable(root.querySelector('#sessions-table'), {
    col: initCol,
    dir: initDir,
    onChange: (col, dir) => {
      const p = new URLSearchParams();
      if (col !== 0 || dir !== 'desc') { p.set('sort', col); p.set('dir', dir); }
      const q = p.toString();
      history.replaceState(null, '', '#/sessions' + (q ? '?' + q : ''));
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
        <span>Session <span class="blur-sensitive">${fmt.htmlSafe(id)}</span></span>
        <span class="spacer"></span>
        <a href="#/sessions" class="muted" style="white-space:nowrap">← all sessions</a>
      </h2>
      <div class="flex muted" style="font-family:var(--mono);font-size:12px;flex-wrap:wrap;gap:14px">
        <span class="blur-sensitive">${fmt.htmlSafe(project)}</span>
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
          <th>prompt / tools</th>
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
