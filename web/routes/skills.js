import { api, fmt, makeSortable, cacheGet, cacheSet } from '/web/app.js';
import { barChart, groupedBarChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}

function writeRange(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/skills';
  location.hash = '#' + base + '?range=' + encodeURIComponent(key);
}

function sinceIso(range) {
  if (!range.days) return null;
  return new Date(Date.now() - range.days * 86400 * 1000).toISOString();
}

function buildUrl(range) {
  const since = sinceIso(range);
  return '/api/skills' + (since ? '?since=' + encodeURIComponent(since) : '');
}

export default async function (root) {
  const range = readRange();
  const url   = buildUrl(range);

  const cached = cacheGet(url);
  if (cached) { renderSkills(root, cached, range); return; }

  const fresh = await api(url);
  cacheSet(url, fresh);
  renderSkills(root, fresh, range);
}

function renderSkills(root, skills, range) {
  // Two split totals: "You ran" sums distinct slash-command sessions across
  // skills (via attribution_skill); "Claude invoked" sums Skill tool_use
  // blocks Claude emitted in Task/Agent-dispatched subagents.
  const totalManual = skills.reduce((s, r) => s + (r.manual_sessions  || 0), 0);
  const totalTool   = skills.reduce((s, r) => s + (r.tool_invocations || 0), 0);

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Skills &amp; Commands</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      ${rangeTabs}
    </div>

    <div class="row cols-3">
      <div class="card kpi"><div class="label">Unique skills / commands</div><div class="value">${fmt.int(skills.length)}</div></div>
      <div class="card kpi"><div class="label">You ran <span class="muted" style="font-weight:400;font-size:11px">(slash commands)</span></div><div class="value">${fmt.int(totalManual)}</div></div>
      <div class="card kpi"><div class="label">Claude invoked <span class="muted" style="font-weight:400;font-size:11px">(Skill tool)</span></div><div class="value">${fmt.int(totalTool)}</div></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Top skills &amp; commands</h3>
      <div id="ch-skills" style="height:320px"></div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>All skills &amp; commands</h3>
      <p class="muted" style="margin:-4px 0 14px;font-size:12px">
        <strong>You ran</strong> = distinct sessions where you typed the slash command (tracked via Claude Code's <code>attributionSkill</code> field on assistant turns).
        <strong>Claude invoked</strong> = times Claude called the Skill tool mid-conversation (typically from Task/Agent-dispatched subagents).
        <strong>Tokens per call</strong> = size of the skill's <code>SKILL.md</code> file loaded into context per invocation (slash commands show — when no matching SKILL.md exists in the catalog).
        <strong>Budget</strong> / <strong>p50 out</strong> / <strong>p95 out</strong> track the skill's <em>output</em> footprint: budget is parsed from the <code>SKILL.md</code> body; p50/p95 sum <code>output_tokens</code> from the Skill call until the user types again or another Skill runs, excluding sidechain subagents.
        Red means p50 exceeds budget by more than 20%.
        <strong>Total $</strong> is the cost the skill itself emitted across this range. <strong>Total inc. subagents</strong> adds the cost of any <code>Task</code>/<code>Agent</code>-dispatched subagents whose parent chain traces back into the skill's window.
      </p>
      <table id="skills-table">
        <thead><tr>
          <th>skill / command</th>
          <th class="num">you ran</th>
          <th class="num">claude invoked</th>
          <th class="num">tokens per call</th>
          <th class="num">budget</th>
          <th class="num">p50 out</th>
          <th class="num">p95 out</th>
          <th class="num">total $</th>
          <th class="num">total inc. subagents</th>
          <th class="num">sessions</th>
          <th>last used</th>
        </tr></thead>
        <tbody>
          ${[...skills].sort((a, b) => ((b.total_with_subagents_usd ?? b.total_cost_usd) || 0) - ((a.total_with_subagents_usd ?? a.total_cost_usd) || 0)).map(s => `
            <tr>
              <td data-val="${fmt.htmlSafe(s.skill)}"><span class="badge">${fmt.htmlSafe(s.skill)}</span></td>
              <td class="num" data-val="${s.manual_sessions  || 0}">${s.manual_sessions  ? fmt.int(s.manual_sessions)  : '<span class="muted">—</span>'}</td>
              <td class="num" data-val="${s.tool_invocations || 0}">${s.tool_invocations ? fmt.int(s.tool_invocations) : '<span class="muted">—</span>'}</td>
              <td class="num" data-val="${s.tokens_per_call ?? ''}">${s.tokens_per_call == null ? '<span class="muted">—</span>' : fmt.int(s.tokens_per_call)}</td>
              <td class="num" data-val="${s.budget_output_tokens ?? ''}">${s.budget_output_tokens == null ? '<span class="muted">—</span>' : fmt.int(s.budget_output_tokens)}</td>
              <td class="num" data-val="${s.p50_output_tokens ?? ''}">${s.p50_output_tokens == null ? '<span class="muted">—</span>' : (s.over_budget ? `<span class="badge" style="background:#7a2e2e;color:#fff">${fmt.int(s.p50_output_tokens)}</span>` : fmt.int(s.p50_output_tokens))}</td>
              <td class="num" data-val="${s.p95_output_tokens ?? ''}">${s.p95_output_tokens == null ? '<span class="muted">—</span>' : fmt.int(s.p95_output_tokens)}</td>
              <td class="num blur-sensitive" data-val="${s.total_cost_usd ?? ''}">${s.total_cost_usd == null ? '<span class="muted">—</span>' : fmt.usd(s.total_cost_usd)}${s.cost_estimated ? '<span class="muted" title="pricing estimated from model tier">*</span>' : ''}</td>
              <td class="num blur-sensitive" data-val="${s.total_with_subagents_usd ?? ''}">${s.total_with_subagents_usd == null ? '<span class="muted">—</span>' : (s.subagent_cost_usd ? `<span title="own ${fmt.usd(s.total_cost_usd || 0)} + subagents ${fmt.usd(s.subagent_cost_usd)}">${fmt.usd(s.total_with_subagents_usd)}</span>` : fmt.usd(s.total_with_subagents_usd))}</td>
              <td class="num" data-val="${s.sessions || 0}">${fmt.int(s.sessions)}</td>
              <td class="mono" data-val="${s.last_used || ''}">${fmt.ts(s.last_used)}</td>
            </tr>`).join('') || '<tr><td colspan="11" class="muted">no skills or commands used in this range</td></tr>'}
        </tbody>
      </table>
    </div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });

  // Default sort: by "total inc. subagents" (col 8) descending. The fork's
  // editorial line is "expensive things rise to the top"; the new You-ran /
  // Claude-invoked columns are clickable but not the initial pivot.
  const skillsTable = root.querySelector('#skills-table');
  if (skillsTable) makeSortable(skillsTable, { col: 8, dir: 'desc' });

  // Top 12 by combined activity (manual + tool) for the chart — using cost
  // here would surface different rows than the manual/tool labels suggest.
  const top = [...skills]
    .sort((a, b) => ((b.manual_sessions || 0) + (b.tool_invocations || 0))
                  - ((a.manual_sessions || 0) + (a.tool_invocations || 0)))
    .slice(0, 12);
  const anyManual = top.some(t => t.manual_sessions);
  const anyTool   = top.some(t => t.tool_invocations);

  if (anyManual && anyTool) {
    groupedBarChart(document.getElementById('ch-skills'), {
      categories: top.map(t => t.skill.length > 26 ? t.skill.slice(0, 25) + '…' : t.skill),
      series: [
        { name: 'You ran',        values: top.map(t => t.manual_sessions  || 0), color: '#3FB68B' },
        { name: 'Claude invoked', values: top.map(t => t.tool_invocations || 0), color: '#7C5CFF' },
      ],
    });
  } else {
    // Single-series fallback when only one of the two categories has data.
    barChart(document.getElementById('ch-skills'), {
      categories: top.map(t => t.skill.length > 26 ? t.skill.slice(0, 25) + '…' : t.skill),
      values:     top.map(t => (t.manual_sessions || 0) + (t.tool_invocations || 0)),
      color: anyManual ? '#3FB68B' : '#7C5CFF',
    });
  }
}
