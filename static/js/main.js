/* ==========================================================================
   GridMap UI - Client Interactions (Theme, Upload Progress, Polling, Filter)
   ========================================================================== */

// --- 1. Theme Switcher (Auto -> Light -> Dark) ---
function initTheme() {
  const saved = localStorage.getItem('theme') || 'auto';
  applyTheme(saved);
}

function applyTheme(theme) {
  const btn = document.getElementById('themeBtn');
  if (theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
    if (btn) btn.innerHTML = '🌙 Dark';
  } else if (theme === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    if (btn) btn.innerHTML = '☀️ Light';
  } else {
    document.documentElement.removeAttribute('data-theme');
    if (btn) btn.innerHTML = '🖥 Auto';
  }
}

function cycleTheme() {
  const current = localStorage.getItem('theme') || 'auto';
  const next = current === 'auto' ? 'light' : current === 'light' ? 'dark' : 'auto';
  localStorage.setItem('theme', next);
  applyTheme(next);
}

// --- 2. Live Table Search Filter ---
function filterTable(input, tableId) {
  const query = input.value.toLowerCase().trim();
  const rows = document.querySelectorAll('#' + tableId + ' tbody tr');
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(query) ? '' : 'none';
  });
}

// --- 3. Format Bytes ---
function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

// --- 4. File Dropzones & Selection Helpers ---
function setupDropzones() {
  document.querySelectorAll('.dropzone').forEach(zone => {
    const input = zone.querySelector('input[type="file"]');
    const display = zone.querySelector('.file-list');
    if (!input || !display) return;

    ['dragenter', 'dragover'].forEach(eventName => {
      zone.addEventListener(eventName, e => {
        e.preventDefault();
        zone.classList.add('dragover');
      }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
      zone.addEventListener(eventName, e => {
        e.preventDefault();
        zone.classList.remove('dragover');
      }, false);
    });

    zone.addEventListener('drop', e => {
      if (e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        updateFileList(input, display);
      }
    });

    input.addEventListener('change', () => {
      updateFileList(input, display);
    });
  });
}

function updateFileList(input, display) {
  if (!input.files || input.files.length === 0) {
    display.textContent = '';
    return;
  }
  const names = Array.from(input.files).map(f => `${f.name} (${formatBytes(f.size)})`);
  display.textContent = 'Selected: ' + names.join(', ');
}

// --- 5. Upload Progress Bar with XMLHttpRequest ---
function setupUploadForm() {
  const form = document.getElementById('uploadForm');
  if (!form) return;

  form.addEventListener('submit', function (e) {
    e.preventDefault();

    const progressContainer = document.getElementById('uploadProgress');
    const progressFill = document.getElementById('progressFill');
    const progressPercent = document.getElementById('progressPercent');
    const progressStatus = document.getElementById('progressStatus');
    const submitBtn = document.getElementById('submitBtn');

    if (progressContainer) progressContainer.style.display = 'block';
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = '⏳ Uploading files...';
    }

    const formData = new FormData(form);
    const xhr = new XMLHttpRequest();

    xhr.upload.addEventListener('progress', function (event) {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 100);
        if (progressFill) progressFill.style.width = percent + '%';
        if (progressPercent) progressPercent.textContent = percent + '%';
        if (progressStatus) {
          progressStatus.textContent = `Uploaded ${formatBytes(event.loaded)} of ${formatBytes(event.total)}`;
        }
      }
    });

    xhr.addEventListener('load', function () {
      if (xhr.status >= 200 && xhr.status < 400) {
        if (progressStatus) progressStatus.textContent = 'Upload complete! Starting analysis...';
        // If response is a redirect or returns a target URL
        if (xhr.responseURL) {
          window.location.href = xhr.responseURL;
        } else {
          try {
            const res = JSON.parse(xhr.responseText);
            if (res.redirect) window.location.href = res.redirect;
            else window.location.reload();
          } catch (err) {
            window.location.reload();
          }
        }
      } else {
        alert('Upload failed: ' + (xhr.statusText || 'Server error (' + xhr.status + ')'));
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Run all checks';
        }
      }
    });

    xhr.addEventListener('error', function () {
      alert('An error occurred during file upload. Please check network connection.');
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Run all checks';
      }
    });

    xhr.open('POST', form.action, true);
    xhr.send(formData);
  });
}

// --- 6. Live Progress Poller for Running Job ---
function startJobPolling(statusUrl) {
  const logBox = document.getElementById('logBox');
  const stepIngest = document.getElementById('step-ingest');
  const stepChecks = document.getElementById('step-checks');
  const stepBreakdown = document.getElementById('step-breakdown');
  const stepTopology = document.getElementById('step-topology');
  const stepReports = document.getElementById('step-reports');

  async function poll() {
    try {
      const res = await fetch(statusUrl);
      if (!res.ok) throw new Error('Status poll failed');
      const data = await res.json();

      if (logBox && data.log) {
        logBox.textContent = data.log.join('\n');
        logBox.scrollTop = logBox.scrollHeight;
      }

      // Update step tracker based on log cues
      const logText = (data.log || []).join(' ');
      
      if (logText.includes('Reading MI') || logText.includes('Saved to event store')) {
        setStep(stepIngest, 'done');
        setStep(stepChecks, 'active');
      }
      if (logText.includes('data checks') || logText.includes('checks:')) {
        setStep(stepChecks, 'done');
        setStep(stepBreakdown, 'active');
      }
      if (logText.includes('1/3') || logText.includes('Breakdown-time check')) {
        setStep(stepChecks, 'done');
        setStep(stepBreakdown, 'active');
      }
      if (logText.includes('2/3') || logText.includes('Mapping check from outage')) {
        setStep(stepBreakdown, 'done');
        setStep(stepTopology, 'active');
      }
      if (logText.includes('3/3') || logText.includes('combined results workbook')) {
        setStep(stepTopology, 'done');
        setStep(stepReports, 'active');
      }

      if (data.state !== 'running') {
        if (stepReports) setStep(stepReports, 'done');
        setTimeout(() => window.location.reload(), 800);
      } else {
        setTimeout(poll, 1800);
      }
    } catch (err) {
      setTimeout(poll, 3000);
    }
  }

  function setStep(el, state) {
    if (!el) return;
    el.classList.remove('active', 'done');
    if (state) el.classList.add(state);
  }

  poll();
}

// --- 6. Click an ID -> trace it to the exact input file, worksheet and row ---
// Any table cell under an ID column (consumer / account no, meter id, DT code, feeder code,
// breakdown) is clickable. The server looks the value up in the run's provenance index
// (provenance.py) and lists every input-file row it came from; "Open in Excel" opens the
// original input file at that row (on the server's own PC) or downloads a copy that does.
const TRACE_COLS = new Set(['consumerno', 'consumernumber', 'accountnumber', 'meterid', 'feedermeterid',
  'dtrmeterid', 'newmeterserialno', 'dtcode', 'feedercode', 'recordedfeeder', 'suggestedfeeder',
  'recfeeder', 'recdt', 'recordeddt', 'suggesteddt', 'feederindtrmi', 'feederinconsumermi', 'breakdown']);

function traceConfig() {
  const el = document.getElementById('traceConfig');
  return el ? { trace: el.dataset.trace, open: el.dataset.open } : null;
}

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function traceValueOf(td) {
  if (!td || td.tagName !== 'TD') return null;
  const table = td.closest('table');
  if (!table || table.closest('#traceModal')) return null;
  const th = table.querySelectorAll('thead tr:last-child th')[td.cellIndex];
  if (!th || !TRACE_COLS.has(th.textContent.toLowerCase().replace(/[^a-z0-9]/g, ''))) return null;
  const v = td.textContent.replace(/⌖/g, '').trim();
  return v && !/^(nan|none|n\/a|-)$/i.test(v) ? v : null;
}

function traceModal() {
  let m = document.getElementById('traceModal');
  if (m) return m;
  m = document.createElement('div');
  m.id = 'traceModal';
  m.className = 'trace-modal';
  m.innerHTML = `<div class="trace-dialog" role="dialog" aria-modal="true" aria-labelledby="traceTitle">
      <div class="trace-head"><h3 id="traceTitle">Source rows</h3>
        <button type="button" class="trace-close" aria-label="Close">&times;</button></div>
      <div class="trace-body"></div></div>`;
  document.body.appendChild(m);
  m.addEventListener('click', e => { if (e.target === m || e.target.closest('.trace-close')) m.classList.remove('open'); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') m.classList.remove('open'); });
  m.querySelector('.trace-body').addEventListener('click', e => {
    const b = e.target.closest('button[data-src]');
    if (b) openSourceRow(b);
  });
  return m;
}

async function openTrace(value) {
  const cfg = traceConfig();
  if (!cfg || !value) return;
  const m = traceModal();
  const body = m.querySelector('.trace-body');
  m.querySelector('#traceTitle').textContent = `Source of ${value}`;
  m.classList.add('open');
  body.innerHTML = '<p class="muted">Looking up source rows&hellip;</p>';
  for (let tries = 0; tries < 300; tries++) {
    let d;
    try {
      const res = await fetch(`${cfg.trace}?q=${encodeURIComponent(value)}`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      d = await res.json();
    } catch (err) {
      body.innerHTML = `<p class="bad-text">Could not trace this value: ${escHtml(err.message)}</p>`;
      return;
    }
    if (!m.classList.contains('open')) return;
    if (d.state === 'building') {
      body.innerHTML = `<p class="muted">Indexing this run's input files for tracing (first time only, about a minute)&hellip;<br>
        <small>${escHtml(d.status)}</small></p>`;
      await new Promise(r => setTimeout(r, 2000));
      continue;
    }
    body.innerHTML = renderTrace(d);
    return;
  }
}

function renderTrace(d) {
  if (!d.groups || d.groups.length === 0) {
    return `<p>No row in this run's input files contains <strong>${escHtml(d.query)}</strong>.</p>
      <p class="muted">Traced sources: the MI workbook sheets the analysis reads (Total Consumer MI Data, Total DTR MI,
      Total Feeder MI) and the outage reports uploaded with this run.</p>`;
  }
  const how = d.local_excel
    ? 'Open in Excel opens the input file on this PC (read-only) with the row selected.'
    : 'Open in Excel downloads the input file, saved so Excel opens on that row.';
  let html = `<p class="muted trace-note">Row numbers are the rows you see in Excel. ${how}</p>`;
  for (const g of d.groups) {
    const where = g.sheet && !g.csv ? `${escHtml(g.file)} <span class="trace-sep">&rsaquo;</span> ${escHtml(g.sheet)}` : escHtml(g.file);
    const kind = g.reference ? `referenced in the <em>${escHtml(g.rows[0] ? g.rows[0].column : '')}</em> column` : escHtml(g.role);
    const count = g.total > g.rows.length ? `${g.total.toLocaleString()} rows, first ${g.rows.length} shown` : `${g.total.toLocaleString()} row${g.total === 1 ? '' : 's'}`;
    const hasStart = g.rows.some(r => r.start);
    html += `<div class="trace-group${g.reference ? ' trace-ref' : ''}">
      <div class="trace-file"><span class="trace-path">${where}</span>
        <span class="pill ${g.reference ? 'info' : 'ok'}">${kind}</span> <span class="muted">${count}</span></div>
      <div class="table-container"><table class="data-table trace-table"><thead><tr><th>Row</th><th>Cell</th><th>Column</th>
        ${hasStart ? '<th>Event start</th>' : ''}<th></th><th></th></tr></thead><tbody>`;
    for (const r of g.rows) {
      const used = (g.role === 'MI master' && !g.reference)
        ? (r.used ? '<span class="pill ok" title="The analysis keeps the last row for each key; this is it.">used by analysis</span>'
          : '<span class="pill warn" title="A later row with the same key overrides this one.">duplicate, not used</span>')
        : '';
      html += `<tr><td><strong>${r.row.toLocaleString()}</strong></td><td>${escHtml(r.cell)}</td><td>${escHtml(r.column)}</td>
        ${hasStart ? `<td>${escHtml(r.start || '')}</td>` : ''}<td>${used}</td>
        <td><button type="button" class="btn trace-open" data-src="${g.src}" data-row="${r.row}" data-col="${r.col}">Open in Excel</button></td></tr>`;
    }
    html += '</tbody></table></div></div>';
  }
  return html;
}

async function openSourceRow(btn) {
  const cfg = traceConfig();
  const label = 'Open in Excel';
  btn.disabled = true;
  btn.textContent = 'Opening…';
  try {
    const body = new URLSearchParams({ src: btn.dataset.src, row: btn.dataset.row, col: btn.dataset.col });
    const res = await fetch(cfg.open, { method: 'POST', body });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();
    if (d.opened) {
      btn.textContent = 'Opened ✓';
      btn.title = `Opened ${d.path} at ${d.where}`;
    } else if (!d.download) {
      btn.textContent = 'Not downloadable';
      btn.title = d.error || '';
      alert(d.error || 'This file is not available for download.');
    } else {
      const a = document.createElement('a');
      a.href = d.download;
      document.body.appendChild(a); a.click(); a.remove();
      btn.textContent = 'Downloaded ✓';
      if (d.error) btn.title = 'Excel could not be opened on the server PC: ' + d.error;
    }
  } catch (err) {
    btn.textContent = 'Failed';
    btn.title = err.message;
  }
  setTimeout(() => { btn.disabled = false; btn.textContent = label; }, 4000);
}

function setupTraceCells() {
  if (!traceConfig()) return;
  // Mark ID cells on hover (works for tables rendered later by JS, e.g. the Raw Data Explorer).
  document.addEventListener('mouseover', e => {
    const td = e.target.closest && e.target.closest('td');
    if (!td || td.dataset.traceChecked) return;
    td.dataset.traceChecked = '1';
    const v = traceValueOf(td);
    if (!v) return;
    td.classList.add('traceable');
    if (td.querySelector('a')) {           // keep existing links (feeder drill-down); add a trace button
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'trace-btn'; b.textContent = '⌖';
      b.title = `Trace ${v} to its source file, sheet and row`;
      td.appendChild(b);
    } else {
      td.title = `Click to trace ${v} to its source file, sheet and row`;
    }
  });
  document.addEventListener('click', e => {
    const q = e.target.closest && e.target.closest('[data-trace-q]');
    if (q) { openTrace(q.dataset.traceQ); return; }
    const td = e.target.closest && e.target.closest('td.traceable');
    if (!td) return;
    if (e.target.closest('a') || (e.target.closest('button') && !e.target.closest('.trace-btn'))) return;
    if (String(window.getSelection && window.getSelection()) !== '') return;   // user is selecting text
    e.preventDefault();
    openTrace(traceValueOf(td));
  });
}

// Global initialization
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  setupDropzones();
  setupUploadForm();
  setupTraceCells();
});
