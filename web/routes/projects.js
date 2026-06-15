import { api, fmt, makeSortable, cacheGet, cacheSet } from '/web/app.js';

const URL = '/api/projects';

export default async function (root) {
  const cached = cacheGet(URL);
  if (cached) { renderProjects(root, cached); return; }

  const fresh = await api(URL);
  cacheSet(URL, fresh);
  renderProjects(root, fresh);
}

function renderProjects(root, rows) {
  root.innerHTML = `
    <div class="card">
      <h2>Projects</h2>
      <p class="muted" style="margin:-8px 0 14px">Click any column header to sort. Cache reads are billed cheaper, so high cache-read columns are good.</p>
      <table id="projects-table">
        <thead><tr><th>project</th><th class="num">sessions</th><th class="num">turns</th><th class="num">billable tokens</th><th class="num">cache reads</th></tr></thead>
        <tbody>
          ${rows.map(r => `
            <tr>
              <td class="blur-sensitive" data-val="${fmt.htmlSafe(r.project_name || r.project_slug)}" title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num" data-val="${r.sessions || 0}">${fmt.int(r.sessions)}</td>
              <td class="num" data-val="${r.turns || 0}">${fmt.int(r.turns)}</td>
              <td class="num" data-val="${r.billable_tokens || 0}">${fmt.int(r.billable_tokens)}</td>
              <td class="num" data-val="${r.cache_read_tokens || 0}">${fmt.int(r.cache_read_tokens)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  makeSortable(root.querySelector('#projects-table'), { col: 3, dir: 'desc' });
}
