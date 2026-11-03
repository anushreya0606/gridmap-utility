# GridMap: Feeder–DTR–Consumer Mapping Check Website

A web application and analysis engine for electricity distribution utilities that verifies whether consumers, DTRs (distribution transformers), and feeders are correctly mapped on the network using outage timing telemetry.

---

## Key Features

1. **Deterministic Topology Analysis Engine:**
   - **Primary Breakdown Check:** Checks Feeder &rarr; DTR, Feeder &rarr; Consumer, and DTR &rarr; Consumer match/mismatch within each feeder breakdown window (&plusmn;2 min tolerance).
   - **Topology Engine:** Dual-edge (start + restore) matching, Jaccard similarity scoring, and virtual peer-group validation for unmetered DTRs.
   - **DTR Verdicts:** Classifies every DTR in the circle as *CORRECT*, *WRONG*, *CONFLICT*, *FEEDER METER ISSUE*, *UNCLEAR*, or *NOT CHECKABLE*.

2. **Modern Web UI:**
   - **Upload Dashboard:** Drag-and-drop file upload with a real-time upload progress bar (MB transferred, percentage).
   - **Event Scope:** Choose uploaded files only, all stored events, last N days, or custom date range.
   - **Remembered MI Master:** MI master workbook is remembered so you only re-upload when it changes.
   - **Executive Dashboard:** Client headline KPIs prominently displayed at the top with responsive SVG comparison charts.
   - **Contained Tables:** All large tables scroll horizontally and vertically within contained boxes with sticky headers and instant search filtering.
   - **Feeder Drilldown:** Click any feeder code to inspect all its DTRs with individual match/mismatch counts.
   - **Light & Dark Mode:** Auto/Light/Dark toggle persisted per browser.

3. **4 Comprehensive Excel Downloads:**
   - `All_Results.xlsx` (combined master workbook with headline stacked summaries and all analysis sheets)
   - `Breakdown_Mapping_Check.xlsx` (feeder breakdown check details)
   - `Mapping_Report.xlsx` (wrong-mapping consumer suggestions and DTR verdicts)
   - `Feeder_DTR_Consumer.xlsx` (hierarchical outage tree and corrected mapping)

4. **Privacy & Data Security:**
   - **Zero Login:** In public mode (`GRIDMAP_PUBLIC=1`), each browser receives an isolated private workspace via an encrypted cookie.
   - **Automatic Cleanup:** Inactive workspaces auto-purged after `GRIDMAP_KEEP_DAYS` (default 7 days).
   - **Consumer Protection:** Personal names and street addresses are permanently stripped from web views and downloads.

---

## File Structure

| File / Folder | Role |
|---|---|
| `app.py` | Flask web application (upload handler, job manager, background worker, API routes) |
| `templates/` | Modular Jinja2 templates (`base.html`, `index.html`, `job_results.html`, `job_progress.html`, `feeder_detail.html`, `history.html`, `stored.html`, `method.html`) |
| `static/` | Modern CSS (`style.css`) supporting light/dark modes and client interactions (`main.js`) |
| `topology_check.py` | Core engine: event matching, scoring, virtual peer groups, DTR verdicts |
| `breakdown_check.py` | Client check: Feeder &rarr; DTR, Feeder &rarr; Consumer, DTR &rarr; Consumer match within breakdown windows |
| `build_hierarchy.py` | Feeder &rarr; DTR &rarr; Consumer &rarr; time Excel workbook builder |
| `run_check.py` | Command-line report writer and report column classifier |
| `start_public_website.bat` | 1-click script to start the website and create a free public HTTPS link via Cloudflare Tunnel |
| `start_server.bat` | 1-click script to run on local or office network |
| `start_website.bat` | Local computer launcher |
| `Dockerfile` | Container definition for Docker / Cloud deployment |
| `DEPLOY.md` | Deployment instructions for Cloudflare Tunnel, Hugging Face Spaces, Render, and on-premise VMs |

---

## Quick Start (Local Windows)

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Launch the site:
   ```powershell
   python app.py
   ```
   Open `http://127.0.0.1:5000` in your browser.

---

## Instant Free Public Deployment

To generate a live public HTTPS link right now without signing up anywhere:
```powershell
.\start_public_website.bat
```
This runs the local engine and launches a secure tunnel, printing a public `https://...trycloudflare.com` URL in seconds.

---

## Automated Verification Tests

Verify that analysis calculations and web routes remain exact:
```powershell
# 1. Core numbers baseline verification (ensures zero regression on test data)
python test_baseline.py

# 2. Flask web routes and privacy check
python test_app_routes.py

# 3. End-to-end job generation and download tests
python test_e2e_job.py
```
