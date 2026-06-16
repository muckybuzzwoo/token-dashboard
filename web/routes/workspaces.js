import { api, fmt } from '/web/app.js';
import { sankeyChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = q.match(/(?:^|&)range=([^&]+)/);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  // Hardcoded base — the previous "re-extract from current hash" pattern was
  // a fragile DOM-injection seed even though browsers don't execute fragment
  // assignments. Audit MEDIUM/LOW guidance: keep route bases as literals.
  location.hash = '#/workspaces?range=' + encodeURIComponent(key);
}

function sinceIso(range) {
  if (!range.days) return null;
  return new Date(Date.now() - range.days * 86400 * 1000).toISOString();
}

export default async function (root) {
  const range = readRange();
  const since = sinceIso(range);
  const qs = since ? '?since=' + encodeURIComponent(since) : '';
  const [matrix, leaks] = await Promise.all([
    api('/api/workspaces' + qs),
    api('/api/cross-workspace-leaks' + qs),
  ]);

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Workspaces</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      ${rangeTabs}
    </div>

    <div class="row cols-4">
      <div class="card kpi"><div class="label">Workspaces touched</div><div class="value">${fmt.int(matrix.nodes.length / 2)}</div></div>
      <div class="card kpi"><div class="label">File-touching calls</div><div class="value">${fmt.int(matrix.total_calls)}</div></div>
      <div class="card kpi"><div class="label">Cross-workspace</div><div class="value">${fmt.int(matrix.cross_workspace_calls)}</div></div>
      <div class="card kpi"><div class="label">Within-workspace</div><div class="value">${fmt.int(matrix.self_loop_calls)}</div></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Workspace flow — agent cwd (left) → file target (right)</h3>
      <details class="glossary" style="margin:-4px 0 14px">
        <summary><span style="font-size:12px">Spots when work done in one project touched files in another</span><span class="muted" style="font-size:12px">— click to expand</span></summary>
        <dl>
          <dt>Left side</dt><dd>The project folder the session was running in.</dd>
          <dt>Right side</dt><dd>The project whose files it actually read or edited.</dd>
          <dt>Same-name pair</dt><dd>Normal — the session stayed in its own project (e.g. <code>token-dashboard-mucky (agent) → token-dashboard-mucky (files)</code>).</dd>
          <dt>Cross pair</dt><dd>A session in project A edited files in project B. Worth a look — maybe the wrong directory, or two projects that should be merged.</dd>
          <dt>File tools only</dt><dd>Counts Read/Edit/Write/NotebookEdit (currently: ${matrix.tools_considered.join(', ')}).</dd>
        </dl>
      </details>
      <div id="ch-workspaces" class="blur-sensitive" style="height:560px"></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Top cross-workspace leaks</h3>
      <p class="muted" style="margin:-4px 0 14px;font-size:12px">
        Agent in source workspace touched files in a different workspace.
      </p>
      <table>
        <thead><tr>
          <th>source workspace</th>
          <th>→</th>
          <th>target workspace</th>
          <th class="num">calls</th>
          <th class="num">sessions</th>
          <th>top files</th>
        </tr></thead>
        <tbody>
          ${leaks.length === 0 ? '<tr><td colspan="6" class="muted">no cross-workspace activity in this range</td></tr>' : leaks.map(l => `
            <tr>
              <td><span class="badge blur-sensitive">${fmt.htmlSafe(l.source)}</span></td>
              <td class="muted">→</td>
              <td><span class="badge blur-sensitive">${fmt.htmlSafe(l.target)}</span></td>
              <td class="num">${fmt.int(l.calls)}</td>
              <td class="num">${fmt.int(l.sessions)}</td>
              <td class="mono" style="font-size:11px">
                ${l.top_files.map(f => `<div class="blur-sensitive" title="${fmt.htmlSafe(f.path)}">${fmt.htmlSafe(fmt.short(f.path, 70))} <span class="muted">(${f.n}×)</span></div>`).join('')}
              </td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });

  if (matrix.links.length > 0) {
    sankeyChart(document.getElementById('ch-workspaces'), {
      nodes: matrix.nodes,
      links: matrix.links,
      formatter: v => Number(v).toLocaleString() + ' calls',
    });
  } else {
    document.getElementById('ch-workspaces').innerHTML = '<p class="muted" style="padding:40px;text-align:center">No file-touching activity in this range.</p>';
  }
}
