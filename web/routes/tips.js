import { api, fmt, cacheGet, cacheSet, cacheClear } from '/web/app.js';

const URL = '/api/tips';

export default async function (root) {
  const cached = cacheGet(URL);
  if (cached) { renderTips(root, cached); return; }

  const fresh = await api(URL);
  cacheSet(URL, fresh);
  renderTips(root, fresh);
}

function safeHref(h) {
  if (!h) return '';
  const s = String(h);
  if (s.startsWith('#/')) return s;
  if (s.startsWith('https://') || s.startsWith('http://')) return s;
  return '';
}

function renderLinks(links) {
  if (!Array.isArray(links) || links.length === 0) return '';
  const parts = links.map(l => {
    const href = safeHref(l && l.href);
    const label = fmt.htmlSafe((l && l.label) || '');
    if (!href || !label) return '';
    const external = href.startsWith('http');
    const attrs = external ? ' target="_blank" rel="noopener"' : '';
    const safeHrefAttr = fmt.htmlSafe(href);
    return `<a href="${safeHrefAttr}"${attrs}>${label}${external ? ' ↗' : ' →'}</a>`;
  }).filter(Boolean);
  if (!parts.length) return '';
  return `<div class="tip-links">${parts.join(' &nbsp;·&nbsp; ')}</div>`;
}

function severityClass(sev) {
  return (sev === 'warning' || sev === 'cost' || sev === 'info') ? sev : 'info';
}

function renderInstances(instances) {
  const rows = instances.map(i => {
    const detail = i.detail
      ? `<div class="tip-instance-detail">${fmt.htmlSafe(i.detail)}</div>`
      : '';
    const links = renderLinks(i.links);
    return `
      <li class="tip-instance">
        <div class="tip-instance-main">
          <div class="tip-instance-title blur-sensitive">${fmt.htmlSafe(i.title)}</div>
          ${detail}
          ${links}
        </div>
        <button class="ghost" data-key="${fmt.htmlSafe(i.key)}">dismiss</button>
      </li>`;
  }).join('');
  return `
    <details class="glossary tip-instances">
      <summary><span class="muted" style="font-size:12px">Show all ${instances.length}</span></summary>
      <ul>${rows}</ul>
    </details>`;
}

function renderTip(t) {
  const sev = severityClass(t.severity);
  const grouped = Array.isArray(t.instances) && t.instances.length > 0;
  const savings = (typeof t.estimated_savings_usd === 'number' && t.estimated_savings_usd > 0)
    ? `<span class="muted blur-sensitive" style="font-size:11px">~${fmt.usd(t.estimated_savings_usd)}/wk</span>`
    : '';
  const count = grouped ? `<span class="muted" style="font-size:12px">(${t.instances.length})</span>` : '';
  const headDismiss = grouped ? '' : `<button class="ghost" data-key="${fmt.htmlSafe(t.key)}">dismiss</button>`;
  return `
    <div class="tip tip-${sev}">
      <div class="tip-head">
        <span class="badge badge-${sev}">${fmt.htmlSafe(t.category)}</span>
        <strong class="blur-sensitive">${fmt.htmlSafe(t.title)}</strong>
        ${count}
        ${savings}
        <span class="spacer"></span>
        ${headDismiss}
      </div>
      <p class="tip-body">${fmt.htmlSafe(t.body)}</p>
      ${renderLinks(t.links)}
      ${grouped ? renderInstances(t.instances) : ''}
    </div>`;
}

function renderTips(root, tips) {
  root.innerHTML = `
    <div class="card">
      <h2>Suggestions</h2>
      ${tips.length === 0
        ? '<p class="muted">No suggestions right now. Token Dashboard surfaces patterns weekly — check back after more activity.</p>'
        : `<p class="muted" style="margin:-8px 0 14px">Rule-based pattern detection over the last 7 days (skill budget &amp; CLAUDE.md size are live filesystem checks). Dismissed tips re-appear after 14 days.</p>`}
      ${tips.map(renderTip).join('')}
    </div>`;

  root.querySelectorAll('button[data-key]').forEach(b => {
    b.addEventListener('click', async () => {
      await fetch('/api/tips/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: b.dataset.key }),
      });
      cacheClear();
      location.reload();
    });
  });
}
