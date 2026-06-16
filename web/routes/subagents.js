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
        <details class="glossary" style="margin:-4px 0 14px">
          <summary><span style="font-size:12px">Where your tokens go, by who's doing the work (the bars below)</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
          <dl>
            <dt>main thread</dt><dd>Your actual back-and-forth with Claude — the work you directly drive.</dd>
            <dt>auto-compaction</dt><dd>Background summarizing Claude Code does on its own when a session gets too long to fit in context. You don't trigger it, but it costs tokens — high numbers mean long sessions are getting summarized a lot. No bar here means none happened in the selected range.</dd>
            <dt>Task subagent</dt><dd>Helpers Claude launches (via the Task/Agent tool) for a focused or parallel job. They run their own conversation and bill separately.</dd>
          </dl>
        </details>
        <div id="ch-kind" style="height:340px"></div>
      </div>
      <div class="card">
        <h3>Per-model split by entrypoint</h3>
        <details class="glossary" style="margin:-4px 0 14px">
          <summary><span style="font-size:12px">How each session was started — the client Claude Code recorded</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
          <dl>
            <dt>cli</dt><dd>You ran <code>claude</code> in a terminal.</dd>
            <dt>claude-desktop</dt><dd>You used the Claude Code desktop app (Mac/Windows).</dd>
            <dt>claude-vscode</dt><dd>You used the VS Code / IDE integration.</dd>
            <dt>sdk-*</dt><dd>A script started it, not you — code calling the Agent SDK directly: <code>sdk-py</code> (Python), <code>sdk-ts</code> (TypeScript), <code>sdk-cli</code>. If you see these and didn't write such a script, it's automation running on your account.</dd>
            <dt>other</dt><dd>The value is taken straight from the session record, so any other client label (or a blank one) can show up here too.</dd>
          </dl>
        </details>
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
      <details class="glossary" style="margin:-4px 0 14px">
        <summary><span style="font-size:12px">The numbers behind the charts above — one row per kind × model</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
        <dl>
          <dt>kind</dt><dd>Who did the work — main thread, auto-compaction, or Task subagent (see "Per-model split by agent kind" above).</dd>
          <dt>model</dt><dd>The model that ran.</dd>
          <dt>messages</dt><dd>How many assistant messages this kind × model produced.</dd>
          <dt>sessions</dt><dd>How many distinct sessions it appeared in.</dd>
          <dt>input + output</dt><dd>Fresh input plus generated output tokens — the part billed at the full rate.</dd>
          <dt>cache read</dt><dd>Context re-used from cache, billed ~10× cheaper than fresh input.</dd>
          <dt>cost (est.)</dt><dd>Estimated dollar cost for this row. A <code>~</code> means the price was estimated from the model tier.</dd>
        </dl>
      </details>
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
      <details class="glossary" style="margin:-4px 0 14px">
        <summary><span style="font-size:12px">Each row = one time the main conversation handed a job to a subagent, and what it cost</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
        <dl>
          <dt>dispatcher (main)</dt><dd>The model in your main conversation that decided to delegate.</dd>
          <dt>child (subagent)</dt><dd>The model that actually ran the delegated job.</dd>
          <dt>subagent_type</dt><dd>The kind of agent that was dispatched (e.g. <code>Explore</code>, <code>claude-code-guide</code>), or — if it wasn't tagged.</dd>
          <dt>session</dt><dd>Which session this happened in (first 8 characters; hover for the full id).</dd>
          <dt>workspace</dt><dd>The project the main conversation was working in.</dd>
          <dt>child msgs</dt><dd>How many messages the subagent thread produced.</dd>
          <dt>i/o tokens</dt><dd>The subagent's input + output tokens combined.</dd>
          <dt>child cost</dt><dd>What the subagent thread cost. A <code>~</code> means the price was estimated from the model tier.</dd>
          <dt>when</dt><dd>When the main conversation dispatched the job.</dd>
        </dl>
        <p class="muted" style="margin:10px 0 0;font-size:11px;opacity:0.8">Sorted by child token spend (biggest first). How they're linked: timing — a subagent that starts within ~1s of a dispatch call.</p>
      </details>
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
      <details class="glossary" style="margin:-4px 0 14px">
        <summary><span style="font-size:12px">Sessions where subagent work cost the most</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
        <dl>
          <dt>project</dt><dd>The workspace the session ran in.</dd>
          <dt>session</dt><dd>The session id (first 8 characters; hover for the full id). Click any row to open the full session view.</dd>
          <dt>subagent msgs</dt><dd>How many messages the session's subagents produced.</dd>
          <dt>i/o tokens</dt><dd>Those subagents' input + output tokens combined.</dd>
          <dt>cache read</dt><dd>Cached tokens the subagents reused — context they'd already seen, billed far cheaper than fresh input.</dd>
          <dt>models seen</dt><dd>Which models the subagents ran on.</dd>
        </dl>
      </details>
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
