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

// Global initialization
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  setupDropzones();
  setupUploadForm();
});
