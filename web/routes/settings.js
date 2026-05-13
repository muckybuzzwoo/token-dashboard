import { api, state, $, fmt } from '/web/app.js';

export default async function (root) {
  const cur = await api('/api/plan');
  const settings = await api('/api/settings');
  const plans = Object.entries(cur.pricing.plans);
  const savedClaudeDirs = settings.claude_dirs || [];
  let originalClaudeDir = settings.claude_dir;
  root.innerHTML = `
    <div class="card">
      <h2>Settings</h2>
      <h3 style="margin-top:16px">Plan</h3>
      <p class="muted" style="margin:0 0 12px">Sets how cost is displayed. API mode shows pay-per-token rates. Subscription modes show what you actually pay each month.</p>
      <div class="flex">
        <select id="plan">
          ${plans.map(([k,v]) => `<option value="${k}" ${k===cur.plan?'selected':''}>${v.label}${v.monthly?` — $${v.monthly}/mo`:''}</option>`).join('')}
        </select>
        <button class="primary" id="save">Save</button>
        <span id="msg" class="muted"></span>
      </div>

      <hr class="divider">

      <h3>Claude folder</h3>
      <p class="muted" style="margin:0 0 12px">Set the <code>.claude</code> folder used for transcript scanning. The dashboard scans <code>projects</code> inside this folder. Existing cached dashboard data stays in this SQLite DB unless you clear it before scanning the new folder.</p>
      <div class="flex">
        <span class="combo-input ${savedClaudeDirs.length > 1 ? 'has-trigger' : ''}">
          <input id="claude-dir" type="text" list="claude-dir-options" autocomplete="off" value="${fmt.htmlSafe(settings.claude_dir)}" ${settings.projects_overridden ? 'disabled' : ''}>
          <button class="combo-trigger" id="claude-dir-picker" type="button" title="Show saved folders" aria-label="Show saved Claude folders" ${settings.projects_overridden ? 'disabled' : ''}>▾</button>
        </span>
        <datalist id="claude-dir-options">
          ${savedClaudeDirs.map(p => `<option value="${fmt.htmlSafe(p)}"></option>`).join('')}
        </datalist>
        <button class="primary" id="save-settings" ${settings.projects_overridden ? 'disabled' : ''}>Save</button>
        <span id="settings-msg" class="muted"></span>
      </div>
      <label class="muted" style="display:flex;align-items:flex-start;gap:8px;margin:0 0 10px;max-width:820px">
        <input id="reset-scan-data" type="checkbox" checked ${settings.projects_overridden ? 'disabled' : ''}>
        <span>Start fresh for this folder: remove previously scanned transcript data before the next scan, so usage from other accounts or profiles is not mixed in.</span>
      </label>
      ${settings.projects_overridden ? `<p class="muted" style="margin-top:8px">A launch-time projects directory is active: <code>${fmt.htmlSafe(settings.projects_dir)}</code></p>` : `<p class="muted" style="margin-top:8px">Current scan root: <code>${fmt.htmlSafe(settings.projects_dir)}</code></p>`}

      <hr class="divider">

      <h3>Pricing table</h3>
      <p class="muted" style="margin:0 0 12px">Edit <code>pricing.json</code> in the project root to change rates. Reload the page after editing.</p>
      <table>
        <thead><tr><th>model</th><th class="num">input</th><th class="num">output</th><th class="num">cache read</th><th class="num">cache 5m</th><th class="num">cache 1h</th></tr></thead>
        <tbody>
          ${Object.entries(cur.pricing.models).map(([k,v]) => `
            <tr><td><span class="badge ${v.tier}">${k}</span></td>
              <td class="num">$${v.input.toFixed(2)}</td>
              <td class="num">$${v.output.toFixed(2)}</td>
              <td class="num">$${v.cache_read.toFixed(2)}</td>
              <td class="num">$${v.cache_create_5m.toFixed(2)}</td>
              <td class="num">$${v.cache_create_1h.toFixed(2)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted" style="margin-top:8px;font-size:11px">Rates per 1M tokens, USD.</p>

      <hr class="divider">

      <h3>Privacy</h3>
      <p class="muted">Press <code>Cmd/Ctrl + B</code> anywhere to blur prompt text and other sensitive content for screenshots.</p>
    </div>`;

  $('#save').addEventListener('click', async () => {
    const plan = $('#plan').value;
    await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    state.plan = plan;
    document.getElementById('plan-pill').textContent = plan;
    $('#msg').textContent = 'Saved.';
    $('#msg').style.color = 'var(--good)';
  });

  $('#claude-dir')?.addEventListener('input', e => {
    if (e.target.value.trim() !== originalClaudeDir) {
      $('#reset-scan-data').checked = true;
    }
  });

  $('#claude-dir-picker')?.addEventListener('click', () => {
    const el = $('#claude-dir');
    el.focus();
    if (typeof el.showPicker === 'function') {
      try {
        el.showPicker();
      } catch {
        // Older browsers may expose showPicker but not support it for datalist inputs.
      }
    }
  });

  $('#save-settings').addEventListener('click', async () => {
    const el = $('#claude-dir');
    const reset = $('#reset-scan-data').checked;
    const msg = $('#settings-msg');
    msg.textContent = 'Saving...';
    msg.style.color = 'var(--muted)';
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ claude_dir: el.value, reset_scan_data: reset }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      msg.textContent = err.error || 'Could not save.';
      msg.style.color = 'var(--bad)';
      return;
    }
    const saved = await resp.json();
    originalClaudeDir = saved.claude_dir;
    el.value = saved.claude_dir;
    $('#reset-scan-data').checked = reset;
    const options = $('#claude-dir-options');
    options.innerHTML = (saved.claude_dirs || []).map(p => `<option value="${fmt.htmlSafe(p)}"></option>`).join('');
    $('.combo-input')?.classList.toggle('has-trigger', (saved.claude_dirs || []).length > 1);

    const scanResp = await fetch('/api/scan');
    if (!scanResp.ok) {
      const err = await scanResp.json().catch(() => ({}));
      msg.textContent = err.error || 'Saved, but scan failed.';
      msg.style.color = 'var(--bad)';
      return;
    }
    msg.textContent = reset ? 'Cache cleared, saved, and scanned.' : 'Saved and scanned.';
    msg.style.color = 'var(--good)';
  });
}
