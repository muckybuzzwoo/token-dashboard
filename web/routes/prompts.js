import { api, fmt, cacheGet, cacheSet } from '/web/app.js';
import { toCSV, toMarkdown, copyToClipboard, downloadBlob } from '/web/export.js';

const SORTS = [
  { key: 'tokens', label: 'Most tokens' },
  { key: 'recent', label: 'Most recent' },
];

function readSort() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)sort=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return SORTS.find(s => s.key === k) || SORTS[0];
}

function writeSort(key) {
  // Hardcoded base, not re-extracted from the current hash — matches
  // workspaces.js/subagents.js after the code-review audit.
  location.hash = '#/prompts?sort=' + encodeURIComponent(key);
}

export default async function (root) {
  const sort = readSort();
  const url  = '/api/prompts?limit=100&sort=' + encodeURIComponent(sort.key);

  const cached = cacheGet(url);
  if (cached) { renderPrompts(root, cached, sort); return; }

  const fresh = await api(url);
  cacheSet(url, fresh);
  renderPrompts(root, fresh, sort);
}

function renderPrompts(root, rows, sort) {
  const sortTabs = `
    <div class="range-tabs" role="tablist">
      ${SORTS.map(s => `<button data-sort="${s.key}" class="${s.key === sort.key ? 'active' : ''}">${s.label}</button>`).join('')}
    </div>`;

  const subtitle = sort.key === 'recent'
    ? 'Your latest prompts and the assistant turn each one triggered. Click a row to see the full prompt.'
    : 'The prompts that cost the most tokens. Click a row to see the full prompt.';

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Prompts</h2>
      <div class="spacer"></div>
      ${sortTabs}
      <button id="export-md" type="button" title="Copy this view to the clipboard as Markdown">Copy MD</button>
      <button id="export-csv" type="button" title="Download this view as CSV">Download CSV</button>
    </div>

    <div class="card">
      <details class="glossary glossary--legend" style="margin:0 0 14px">
        <summary><span style="font-size:12px">${subtitle}</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
        <dl>
          <dt>when / cache cost</dt><dd>In <em>Most recent</em> this is when you sent the prompt; in <em>Most tokens</em> it's the <strong>cache cost</strong> — the dollar cost of just the cache-read tokens for that turn. Shows — when the model can't be priced.</dd>
          <dt>prompt</dt><dd>The text you typed. Click any row to see the full prompt.</dd>
          <dt>model</dt><dd>The model that answered this prompt (the first real model in the turn).</dd>
          <dt>tokens</dt><dd>Billable tokens for the whole turn — input + output + cache creation, including the tool loop it triggered (not just the first reply).</dd>
          <dt>cache rd</dt><dd>Cache-read tokens: earlier context re-used from cache, billed ~10× cheaper than fresh input.</dd>
          <dt>session</dt><dd>The session this prompt belongs to; click to open the full session view.</dd>
        </dl>
        <p class="muted" style="margin:10px 0 0;font-size:11px;opacity:0.8">Rows with model <code>&lt;synthetic&gt;</code> and no cost are Claude Code's automatic "Continue from where you left off." turns — placeholders it inserts after a session resume or auto-compaction, not real prompts. They still carry token counts (mostly re-read context), but there's no real model behind them, so no price is shown.</p>
      </details>
      <table id="prompts">
        <thead><tr>
          <th>${sort.key === 'recent' ? 'when' : 'cache cost'}</th>
          <th>prompt</th>
          <th>model</th>
          <th class="num">tokens</th>
          <th class="num">cache rd</th>
          <th>session</th>
        </tr></thead>
        <tbody>
          ${rows.map((r,i) => `
            <tr data-i="${i}" style="cursor:pointer">
              <td class="${sort.key === 'recent' ? 'mono' : 'num mono blur-sensitive'}">${sort.key === 'recent' ? fmt.ts(r.timestamp) : fmt.usd4(r.estimated_cost_usd)}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(fmt.short(r.prompt_text, 110))}</td>
              <td><span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono blur-sensitive" onclick="event.stopPropagation()">${fmt.htmlSafe(r.session_id.slice(0,8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">no prompts yet</td></tr>'}
        </tbody>
      </table>
    </div>
    <div id="drawer"></div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeSort(btn.dataset.sort));
  });

  const sections = [{
    heading: `Prompts — ${sort.label.toLowerCase()}`,
    columns: ['timestamp', 'prompt', 'model', 'billable_tokens', 'cache_read_tokens', 'estimated_cost_usd', 'session_id'],
    rows: rows.map(r => [
      r.timestamp,
      r.prompt_text || '',
      r.model || '',
      r.billable_tokens,
      r.cache_read_tokens,
      r.estimated_cost_usd,
      r.session_id,
    ]),
  }];

  root.querySelector('#export-md').addEventListener('click', async () => {
    const md = toMarkdown(`Prompts (${sort.label.toLowerCase()})`, sections);
    const ok = await copyToClipboard(md);
    const btn = root.querySelector('#export-md');
    const original = btn.textContent;
    btn.textContent = ok ? 'Copied!' : 'Copy failed';
    setTimeout(() => { btn.textContent = original; }, 1500);
  });

  root.querySelector('#export-csv').addEventListener('click', () => {
    const csv = toCSV(sections);
    const stamp = new Date().toLocaleDateString('sv'); // browser-local YYYY-MM-DD
    downloadBlob(`prompts-${sort.key}-${stamp}.csv`, 'text/csv', csv);
  });

  root.querySelectorAll('#prompts tbody tr').forEach(tr => {
    tr.addEventListener('click', () => {
      const r = rows[Number(tr.dataset.i)];
      const drawer = document.getElementById('drawer');
      drawer.innerHTML = `
        <div class="card">
          <h3 style="display:flex;align-items:center">
            <span>Prompt detail</span>
            <span class="spacer"></span>
            <span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>
          </h3>
          <pre class="blur-sensitive">${fmt.htmlSafe(r.prompt_text || '')}</pre>
          <div class="flex" style="margin-top:12px;flex-wrap:wrap;gap:14px">
            <span class="muted">${fmt.ts(r.timestamp)}</span>
            <span class="muted blur-sensitive">${fmt.int(r.billable_tokens)} billable · ${fmt.int(r.cache_read_tokens)} cache rd · ~${fmt.usd4(r.estimated_cost_usd)} cache cost</span>
            <span class="spacer"></span>
            <a href="#/sessions/${encodeURIComponent(r.session_id)}">Open session →</a>
          </div>
        </div>`;
      drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
}
