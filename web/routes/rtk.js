import { api, fmt } from '/web/app.js';

// Sonnet base input price — actual savings vary with cache hits
const USD_PER_TOKEN = 3 / 1_000_000;

function toUsd(tokens) {
  const n = (tokens ?? 0) * USD_PER_TOKEN;
  return n < 0.01 ? '$' + n.toFixed(4) : '$' + n.toFixed(2);
}

function today() {
  // Browser-local day, to match the rtk CLI's local-time daily buckets.
  return new Date().toLocaleDateString('sv');
}

export default async function (root) {
  let data;
  try {
    data = await api('/api/rtk');
  } catch (e) {
    root.innerHTML = `<div class="card"><p class="muted">RTK unavailable: ${fmt.htmlSafe(String(e))}</p></div>`;
    return;
  }

  if (data.available === false) {
    root.innerHTML = `
      <div class="card">
        <h2>RTK Token Savings</h2>
        <p class="muted">RTK is an optional CLI proxy that compresses noisy command output before it reaches your AI coding session. It can reduce token use on common commands like <code>git diff</code>, test runs, and searches.</p>
        <p class="muted" style="margin-top:8px">Install RTK if you want Token Dashboard to show command-output savings alongside Claude usage.</p>
        <p style="margin-top:12px"><a href="${fmt.htmlSafe(data.install_url || 'https://github.com/rtk-ai/rtk')}" target="_blank" rel="noopener noreferrer">View RTK install instructions →</a></p>
      </div>`;
    return;
  }

  const s = data.summary;
  if (!s || s.total_commands === 0) {
    root.innerHTML = `
      <div class="card">
        <h2>RTK Token Savings</h2>
        <p class="muted">RTK is installed, but there is no savings data yet. Run some commands through Claude Code with the RTK hook active, then come back.</p>
        <p class="muted" style="margin-top:8px">Check that <code style="font-family:var(--mono);background:var(--panel-2);padding:2px 6px;border-radius:4px">~/.claude/hooks/rtk-rewrite.sh</code> is installed and Claude Code has been restarted.</p>
      </div>`;
    return;
  }

  const todayRow  = data.daily.find(d => d.date === today());
  const lastMonth = data.monthly[data.monthly.length - 1];
  const days      = data.daily.slice(-30);

  root.innerHTML = `
    <div class="row" style="grid-template-columns:repeat(4,1fr);margin-bottom:16px">
      <div class="card">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">All-Time Saved</div>
        <div style="font-size:28px;font-weight:700;color:var(--good);font-family:var(--mono)">${fmt.compact(s.total_saved)}</div>
        <div class="muted" style="font-size:11px">tokens</div>
      </div>
      <div class="card">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">This Month</div>
        <div style="font-size:28px;font-weight:700;color:var(--good);font-family:var(--mono)">${fmt.compact(lastMonth?.saved_tokens ?? 0)}</div>
        <div class="muted" style="font-size:11px">tokens</div>
      </div>
      <div class="card">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Today</div>
        <div style="font-size:28px;font-weight:700;color:var(--accent);font-family:var(--mono)">${fmt.compact(todayRow?.saved_tokens ?? 0)}</div>
        <div class="muted" style="font-size:11px">tokens</div>
      </div>
      <div class="card">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Avg Compression</div>
        <div style="font-size:28px;font-weight:700;color:var(--accent);font-family:var(--mono)">${s.avg_savings_pct.toFixed(0)}%</div>
        <div class="muted" style="font-size:11px">${fmt.int(s.total_commands)} commands processed</div>
      </div>
    </div>

    <div class="row" style="grid-template-columns:1fr 1fr;margin-bottom:16px">
      <div class="card" style="display:flex;flex-direction:column;justify-content:center">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Est. API Cost Saved</div>
        <div class="blur-sensitive" style="font-size:40px;font-weight:700;color:var(--good);font-family:var(--mono);line-height:1">${toUsd(s.total_saved)}</div>
        <div class="muted blur-sensitive" style="font-size:11px;margin-top:6px">Sonnet · $3 / MTok input · base pricing</div>
        <div class="muted" style="font-size:11px">Actual savings higher with cache hits</div>
      </div>
      <div class="card">
        <div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Monthly Breakdown</div>
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="color:var(--muted-2);font-size:11px;text-transform:uppercase;letter-spacing:.06em">
              <th style="text-align:left;padding:0 0 6px">Month</th>
              <th style="text-align:right;padding:0 0 6px">Saved</th>
              <th style="text-align:right;padding:0 0 6px">%</th>
              <th style="text-align:right;padding:0 0 6px">Commands</th>
              <th style="text-align:right;padding:0 0 6px">Est. USD</th>
            </tr>
          </thead>
          <tbody>
            ${data.monthly.map(m => `
              <tr style="border-top:1px solid var(--border)">
                <td style="padding:6px 0;font-family:var(--mono);font-size:12px">${m.month}</td>
                <td style="padding:6px 0;text-align:right;color:var(--good)">${fmt.compact(m.saved_tokens)}</td>
                <td style="padding:6px 0;text-align:right">${m.savings_pct.toFixed(0)}%</td>
                <td style="padding:6px 0;text-align:right;color:var(--muted)">${fmt.int(m.commands)}</td>
                <td class="blur-sensitive" style="padding:6px 0;text-align:right;color:var(--good);font-family:var(--mono)">${toUsd(m.saved_tokens)}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h2>Daily Savings — Last ${days.length} Days</h2>
      <div id="rtk-chart" style="height:240px"></div>
    </div>`;

  const chart = echarts.init(root.querySelector('#rtk-chart'), 'dark');
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        const bar  = params.find(p => p.seriesName === 'Tokens Saved');
        const line = params.find(p => p.seriesName === 'Savings %');
        return `${params[0].axisValue}<br/>
          Saved: <b>${(bar?.value ?? 0).toLocaleString()}</b> tokens<br/>
          Compression: <b>${line?.value ?? 0}%</b>`;
      },
    },
    grid: { left: 50, right: 50, top: 12, bottom: 36 },
    xAxis: {
      type: 'category',
      data: days.map(d => d.date.slice(5)),
      axisLine: { lineStyle: { color: '#1F2630' } },
      axisLabel: { color: '#5A6573', fontSize: 11 },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: '#5A6573', fontSize: 11, formatter: v => v >= 1000 ? (v / 1000).toFixed(0) + 'k' : v },
        splitLine: { lineStyle: { color: '#1F2630' } },
      },
      {
        type: 'value',
        min: 0, max: 100,
        axisLabel: { color: '#5A6573', fontSize: 11, formatter: v => v + '%' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: 'Tokens Saved',
        type: 'bar',
        data: days.map(d => d.saved_tokens),
        itemStyle: { color: '#3FB68B', borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 40,
      },
      {
        name: 'Savings %',
        type: 'line',
        yAxisIndex: 1,
        data: days.map(d => parseFloat(d.savings_pct.toFixed(1))),
        itemStyle: { color: '#4A9EFF' },
        lineStyle: { width: 2 },
        symbol: 'circle',
        symbolSize: 5,
        smooth: true,
      },
    ],
  });

  window.addEventListener('resize', () => chart.resize());
}
