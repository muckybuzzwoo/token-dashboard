// Drill export helpers — Markdown to clipboard, CSV to download.
// Each caller builds a `sections` payload from the already-fetched drill
// data. Shapes aren't validated here — the caller knows what it rendered.

export function toMarkdown(title, sections) {
  const out = [`# ${title}`, ''];
  for (const s of sections) {
    if (!s.rows?.length) continue;
    out.push(`## ${s.heading}`, '');
    out.push('| ' + s.columns.join(' | ') + ' |');
    out.push('| ' + s.columns.map(() => '---').join(' | ') + ' |');
    for (const r of s.rows) out.push('| ' + r.map(mdCell).join(' | ') + ' |');
    out.push('');
  }
  return out.join('\n');
}

export function toCSV(sections) {
  const lines = [];
  for (const s of sections) {
    if (!s.rows?.length) continue;
    if (lines.length) lines.push('');
    lines.push(`# ${s.heading}`);
    lines.push(s.columns.map(csvCell).join(','));
    for (const r of s.rows) lines.push(r.map(csvCell).join(','));
  }
  return lines.join('\n');
}

export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function downloadBlob(filename, mime, text) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function mdCell(v) {
  if (v == null) return '';
  return String(v).replace(/\|/g, '\\|').replace(/\n/g, ' ');
}

function csvCell(v) {
  if (v == null) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
