import { api, fmt } from '/web/app.js';
import { stackedBarChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

const KIND_COLOR = { main: '#4A9EFF', compact: '#5BCEDA', subagent: '#7C5CFF' };
const KIND_LABEL = {
  main: 'main thread',
  compact: 'auto-compaction',
  subagent: 'Task subagent',
};

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = q.match(/(?:^|&)range=([^&]+)/);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  // Hardcoded base — see workspaces.js for rationale.
  location.hash = '#/subagents?range=' + encodeURIComponent(key);
}

function sinceIso(range) {
  if (!range.days) return null;
  return new Date(Date.now() - range.days * 86400 * 1000).toISOString();
}

function groupBy(rows, dimKey, modelKey = 'model') {
  const dimSet = new Set();
  const modelSet = new Set();
  const cube = {};
  rows.forEach(r => {
    dimSet.add(r[dimKey]); modelSet.add(r[modelKey]);
    cube[r[dimKey]] = cube[r[dimKey]] || {};
    cube[r[dimKey]][r[modelKey]] = (r.input_tokens || 0) + (r.output_tokens || 0);
  });
  const dims = Array.from(dimSet);
  const models = Array.from(modelSet).sort();
  return {
    categories: dims,
    series: models.map(m => ({
      name: fmt.modelShort(m),
      values: dims.map(d => (cube[d] || {})[m] || 0),
    })),
  };
}

export default async function (root) {
  const range = readRange();
  const since = sinceIso(range);
  const qs = since ? '?since=' + encodeURIComponent(since) : '';
  const data = await api('/api/subagents' + qs);

  const rows = data.breakdown;
  const tops = data.top_sessions;
  const byKind = data.by_kind || [];
  const byEntrypoint = data.by_entrypoint || [];
  const sdkRuns = data.sdk_runs || [];
  const tree = data.dispatch_tree || [];

  let totalMainMsgs = 0, totalCompactMsgs = 0, totalSubagentMsgs = 0;
  let totalSubagentCost = 0, totalCompactCost = 0;
  byKind.forEach(r => {
    if (r.kind === 'main')     totalMainMsgs     += r.messages;
    if (r.kind === 'compact')  { totalCompactMsgs  += r.messages; totalCompactCost  += r.cost_usd || 0; }
    if (r.kind === 'subagent') { totalSubagentMsgs += r.messages; totalSubagentCost += r.cost_usd || 0; }
  });
  const totalMsgs = totalMainMsgs + totalCompactMsgs + totalSubagentMsgs;
  const subagentPct = totalMsgs ? (totalSubagentMsgs / totalMsgs * 100).toFixed(0) + '%' : '—';

  const sdkSessions = sdkRuns.reduce((s, r) => s + (r.sessions || 0), 0);
  const sdkMsgs    = sdkRuns.reduce((s, r) => s + (r.messages || 0), 0);

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Subagents &amp; Orchestration</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      ${rangeTabs}
    </div>

    <div class="row cols-3">
      <div class="card kpi"><div class="label">Subagent share</div><div class="value">${subagentPct}</div><div class="sub">${fmt.int(totalSubagentMsgs)} Task-dispatched msgs</div></div>
      <div class="card kpi"><div class="label">Subagent cost (est.)</div><div class="value blur-sensitive">${fmt.usd(totalSubagentCost)}</div><div class="sub blur-sensitive">vs auto-compact ${fmt.usd(totalCompactCost)}</div></div>
      <div class="card kpi"><div class="label">SDK orchestration runs</div><div class="value">${fmt.int(sdkSessions)}</div><div class="sub">${fmt.int(sdkMsgs)} msgs across ${sdkRuns.length} clusters</div></div>
    </div>

    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Per-model split by agent kind</h3>
        <p class="muted" style="margin:-4px 0 14px;font-size:12px">
          Three-way split: <b>main</b> = top-level conversation; <b>auto-compaction</b> = Claude Code's internal context-summarizer (agent_id <code>acompact-*</code>); <b>Task subagent</b> = explicitly dispatched via the Task/Agent tool. The previous "sidechain" bucket lumped compaction + Task together — they have different cost profiles and different fix paths.
        </p>
        <div id="ch-kind" style="height:340px"></div>
      </div>
      <div class="card">
        <h3>Per-model split by entrypoint</h3>
        <p class="muted" style="margin:-4px 0 14px;font-size:12px">
          How the agent was launched: <code>cli</code> (terminal claude), <code>claude-vscode</code> (IDE), <code>sdk-py</code>/<code>sdk-ts</code>/<code>sdk-cli</code> (external orchestration — scripts invoking models directly via the Agent SDK).
        </p>
        <div id="ch-entrypoint" class="blur-sensitive" style="height:340px"></div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>External orchestration runs (SDK entrypoints)</h3>
      <p class="muted" style="margin:-4px 0 14px;font-size:12px">
        Sessions created by the <code>claude_agent_sdk</code> (Python or TypeScript) — typically a script invoking <code>query(model=…)</code> directly rather than going through the interactive CLI. Each row clusters sessions by working directory.
      </p>
      <table>
        <thead><tr>
          <th>entrypoint</th>
          <th>workspace</th>
          <th class="num">sessions</th>
          <th class="num">messages</th>
          <th class="num">i/o tokens</th>
          <th class="num">cache read</th>
          <th>models dispatched</th>
        </tr></thead>
        <tbody>
          ${sdkRuns.length === 0 ? '<tr><td colspan="7" class="muted">no SDK-orchestrated runs in this range</td></tr>' : sdkRuns.map(r => `
            <tr>
              <td><span class="badge blur-sensitive">${fmt.htmlSafe(r.entrypoint)}</span></td>
              <td class="blur-sensitive" title="${fmt.htmlSafe(r.cwd || '')}">${fmt.htmlSafe(r.workspace || r.project_slug)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.messages)}</td>
              <td class="num">${fmt.int(r.io_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td>${(r.models || []).map(m => `<span class="badge model-${fmt.modelClass(m)}">${fmt.htmlSafe(fmt.modelShort(m))}</span>`).join(' ')}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Per-(kind, model) breakdown table</h3>
      <table>
        <thead><tr>
          <th>kind</th>
          <th>model</th>
          <th class="num">messages</th>
          <th class="num">sessions</th>
          <th class="num">input + output</th>
          <th class="num">cache read</th>
          <th class="num">cost (est.)</th>
        </tr></thead>
        <tbody>
          ${byKind.length === 0 ? '<tr><td colspan="7" class="muted">no assistant turns in this range</td></tr>' : byKind.map(r => `
            <tr>
              <td><span class="badge" style="border-color:${KIND_COLOR[r.kind] || '#888'};color:${KIND_COLOR[r.kind] || '#ccc'}">${KIND_LABEL[r.kind] || fmt.htmlSafe(r.kind)}</span></td>
              <td><span class="badge model-${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
              <td class="num">${fmt.int(r.messages)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int((r.input_tokens || 0) + (r.output_tokens || 0))}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td class="num blur-sensitive">${fmt.usd(r.cost_usd)}${r.cost_estimated ? ' <span class="muted">~</span>' : ''}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Dispatch tree — parent prompt → spawned subagent threads</h3>
      <p class="muted" style="margin:-4px 0 14px;font-size:12px">
        Each row is one <code>Agent</code> (formerly <code>Task</code>) tool call: the dispatcher model is the main thread that issued the dispatch; the child models are the subagent thread that ran. Reconstruction is via timing-join on session_id + first-sidechain-after-tool-call (~&lt;1s gap). Sort by total child token spend.
      </p>
      <table>
        <thead><tr>
          <th>dispatcher (main)</th>
          <th>→</th>
          <th>child (subagent)</th>
          <th>subagent_type</th>
          <th>session</th>
          <th>workspace</th>
          <th class="num">child msgs</th>
          <th class="num">i/o tokens</th>
          <th class="num">child cost</th>
          <th>when</th>
        </tr></thead>
        <tbody>
          ${tree.length === 0 ? '<tr><td colspan="10" class="muted">No Agent/Task dispatches in this range.</td></tr>' : tree.map(t => `
            <tr class="clickable" data-session="${fmt.htmlSafe(t.session_id)}">
              <td><span class="badge model-${fmt.modelClass(t.dispatcher_model)}">${fmt.htmlSafe(fmt.modelShort(t.dispatcher_model || 'unknown'))}</span></td>
              <td class="muted">→</td>
              <td>${(t.models || []).map(m => `<span class="badge model-${fmt.modelClass(m)}">${fmt.htmlSafe(fmt.modelShort(m))}</span>`).join(' ')}</td>
              <td class="mono blur-sensitive" style="font-size:11px">${t.subagent_type ? fmt.htmlSafe(t.subagent_type) : '<span class="muted">—</span>'}</td>
              <td class="mono blur-sensitive" style="font-size:11px" title="${fmt.htmlSafe(t.session_id)}">${fmt.htmlSafe(t.session_id.slice(0, 8))}…</td>
              <td class="blur-sensitive">${fmt.htmlSafe(t.project_name || t.project_slug || '')}</td>
              <td class="num">${fmt.int(t.thread_msgs)}</td>
              <td class="num">${fmt.int(t.io_tokens)}</td>
              <td class="num blur-sensitive">${fmt.usd(t.child_cost_usd)}${t.child_cost_estimated ? ' <span class="muted">~</span>' : ''}</td>
              <td class="mono" style="font-size:11px">${fmt.ts(t.dispatched_at)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Top sessions by subagent token spend</h3>
      <p class="muted" style="margin:-4px 0 14px;font-size:12px">
        Click a row to drill into the session view.
      </p>
      <table>
        <thead><tr>
          <th>project</th>
          <th>session</th>
          <th class="num">subagent msgs</th>
          <th class="num">i/o tokens</th>
          <th class="num">cache read</th>
          <th>models seen</th>
        </tr></thead>
        <tbody>
          ${tops.length === 0 ? '<tr><td colspan="6" class="muted">no subagent activity in this range</td></tr>' : tops.map(t => `
            <tr class="clickable" data-session="${fmt.htmlSafe(t.session_id)}">
              <td class="blur-sensitive">${fmt.htmlSafe(t.project_name || t.project_slug)}</td>
              <td class="mono blur-sensitive" style="font-size:11px" title="${fmt.htmlSafe(t.session_id)}">${fmt.htmlSafe(t.session_id.slice(0, 8))}…</td>
              <td class="num">${fmt.int(t.subagent_msgs)}</td>
              <td class="num">${fmt.int(t.io_tokens)}</td>
              <td class="num">${fmt.int(t.cache_read_tokens)}</td>
              <td>${(t.models || []).map(m => `<span class="badge model-${fmt.modelClass(m)}">${fmt.htmlSafe(fmt.modelShort(m))}</span>`).join(' ')}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });
  root.querySelectorAll('tr.clickable').forEach(tr => {
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      location.hash = '#/sessions/' + tr.dataset.session;
    });
  });

  const kindChart = groupBy(byKind, 'kind');
  stackedBarChart(document.getElementById('ch-kind'), {
    categories: kindChart.categories.map(k => KIND_LABEL[k] || k),
    series: kindChart.series,
  });

  const epChart = groupBy(byEntrypoint, 'entrypoint');
  stackedBarChart(document.getElementById('ch-entrypoint'), {
    categories: epChart.categories,
    series: epChart.series,
  });
}
