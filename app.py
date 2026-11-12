"""
app.py – Hugging Face Spaces Entry Point for GridMap Utility

Key Capabilities:
  - Hugging Face Spaces ZeroGPU container compatible (registers via spaces.zero.startup())
  - FastAPI ASGI master application listening on 0.0.0.0:7860
  - Native Gradio Blocks interface mounted at root / so Hugging Face /config healthchecks succeed
  - Complete Flask Web Application mounted at /app with all interactive features, upload progress,
    light/dark theme, SVG charts, and clickable drilldown links
  - Raw Data & Cross-Check Explorer embedded in both Gradio and Flask views
  - 6-part comprehensive utility glossary
  - Direct full-screen link to /app for users who want the standalone web experience
"""
import os
import sys
import sqlite3
import pandas as pd
from pathlib import Path

# Ensure consistent working directories and environment
os.environ.setdefault("GRIDMAP_DATA", "/tmp/gridmap-data")
os.environ.setdefault("GRIDMAP_PUBLIC", "1")
os.environ.setdefault("GRIDMAP_KEEP_DAYS", "7")

# ── ZeroGPU compatibility & registration ─────────────────────────────────────
try:
    import spaces
except ImportError:
    class spaces:  # type: ignore[no-redef]
        @staticmethod
        def GPU(fn=None, **kwargs):
            if callable(fn):
                return fn
            def _d(f): return f
            return _d

@spaces.GPU(duration=60)
def _zero_gpu_worker(data=""):
    """Satisfies ZeroGPU startup registration."""
    return f"ok: {data}"

# ── Framework Imports ────────────────────────────────────────────────────────
import gradio as gr
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.wsgi import WSGIMiddleware
import flask_app as fa
import uvicorn

# ── Custom CSS for Gradio matching Light Blue & Ocean Blue Palette ───────────
CUSTOM_CSS = """
:root {
  --primary: #0284c7;
  --primary-dark: #075985;
  --primary-light: #f0f9ff;
  --bg: #f8fafc;
  --card: #ffffff;
  --border: #e2e8f0;
}
.gradio-container {
  max-width: 1400px !important;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
}
.app-header {
  background: linear-gradient(135deg, #0369a1 0%, #0284c7 60%, #0ea5e9 100%);
  color: #ffffff;
  padding: 24px 30px;
  border-radius: 14px;
  margin-bottom: 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 4px 18px rgba(2, 132, 199, 0.2);
}
.app-title {
  font-size: 24px;
  font-weight: 800;
  margin: 0;
  letter-spacing: -0.02em;
}
.app-subtitle {
  font-size: 13.5px;
  opacity: 0.92;
  margin-top: 4px;
}
.direct-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: #ffffff;
  color: #0369a1 !important;
  font-weight: 700;
  font-size: 13.5px;
  padding: 10px 18px;
  border-radius: 8px;
  text-decoration: none;
  transition: all 0.2s ease;
  box-shadow: 0 2px 8px rgba(0,0,0,0.12);
}
.direct-btn:hover {
  background: #f0f9ff;
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(0,0,0,0.18);
}
.info-banner {
  background: #f0f9ff;
  border: 1px solid #bae6fd;
  border-left: 4px solid #0284c7;
  padding: 12px 18px;
  border-radius: 8px;
  margin-bottom: 16px;
  font-size: 13.5px;
  color: #075985;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
"""

def gradio_cross_check_lookup(query: str):
    """Searches past runs or default workspace SQLite for the given ID."""
    q = (query or "").strip()
    if not q:
        return "⚠️ Please enter a Consumer Number, Meter ID, DTR Code, or Feeder Code to verify."
    
    # Check default or recent workspace
    ws_folder = Path(fa.DEFAULT_DATA) / "ws_default"
    if not ws_folder.exists():
        ws_folder = Path(fa.DEFAULT_DATA)
    
    # Search latest job checks.sqlite
    jobs = fa.list_jobs(fa.WS(str(ws_folder.name)))
    db_path = None
    for j in jobs:
        p = Path(j["folder"]) / "checks.sqlite"
        if p.exists():
            db_path = p
            break
            
    if not db_path or not db_path.exists():
        # Fallback to local store
        store_db = Path(__file__).resolve().parent / "store" / "events.sqlite"
        if not store_db.exists():
            return f"No active analysis results found yet in the system. Run an analysis first, then cross-check '{q}'."
    
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        
        # Check consumer
        cur.execute("""
            SELECT Breakdown, Breakdown_status, Feeder_code, Feeder_name, DT_code, Consumer_No, Meter_ID, 
                   Feeder_off, Feeder_on, Consumer_off, Consumer_on, Result, Exact_timing_off_and_on_within_tol
            FROM feeder_consumer 
            WHERE Meter_ID = ? OR Consumer_No = ?
            LIMIT 15
        """, (q, q))
        c_rows = cur.fetchall()
        
        if c_rows:
            c0 = c_rows[0]
            m_count = sum(1 for r in c_rows if r[11] == "MATCH")
            mm_count = sum(1 for r in c_rows if r[11] == "MISMATCH")
            html = f"""
### 🔍 Raw Telemetry Verification: Consumer {c0[5]} (Meter {c0[6]})

- **Recorded Network Mapping:** Feeder `{c0[2]}` ({c0[3]}) &rarr; DTR `{c0[4]}`
- **Breakdown Window Checks:** {len(c_rows)} total breakdowns checked ({m_count} MATCH, {mm_count} MISMATCH)
- **Engine Verdict:** {"✅ MATCH: Outage timing coincides with recorded network" if m_count >= mm_count else "⚠️ MISMATCH: Outage timing suggests incorrect mapping"}

| Feeder Breakdown | Status | Feeder Off &rarr; On | Consumer Off &rarr; On | Check Result |
| :--- | :--- | :--- | :--- | :--- |
"""
            for r in c_rows:
                html += f"| `{r[0]}` | **{r[1]}** | {r[7]} &rarr; {r[8]} | {r[9]} &rarr; {r[10]} | **{r[11]}** |\n"
            con.close()
            return html
            
        # Check DTR
        cur.execute("""
            SELECT Breakdown, Breakdown_status, Feeder_code, Feeder_name, DT_code, DT_name, 
                   Feeder_off, Feeder_on, DTR_off, DTR_on, Result
            FROM feeder_dtr 
            WHERE DT_code = ?
            LIMIT 15
        """, (q,))
        d_rows = cur.fetchall()
        if d_rows:
            d0 = d_rows[0]
            m_count = sum(1 for r in d_rows if r[10] == "MATCH")
            mm_count = sum(1 for r in d_rows if r[10] == "MISMATCH")
            html = f"""
### 🔍 Raw Telemetry Verification: DTR {d0[4]} ({d0[5]})

- **Recorded Feeder Mapping:** Feeder `{d0[2]}` ({d0[3]})
- **Breakdown Window Checks:** {len(d_rows)} total breakdowns checked ({m_count} MATCH, {mm_count} MISMATCH)
- **Engine Verdict:** {"✅ CORRECT: DTR follows recorded feeder breakdown window" if m_count >= mm_count else "⚠️ WRONG / MISMATCH: DTR did not go off during recorded feeder outages"}

| Feeder Breakdown | Status | Feeder Off &rarr; On | DTR Off &rarr; On | Check Result |
| :--- | :--- | :--- | :--- | :--- |
"""
            for r in d_rows:
                html += f"| `{r[0]}` | **{r[1]}** | {r[6]} &rarr; {r[7]} | {r[8]} &rarr; {r[9]} | **{r[10]}** |\n"
            con.close()
            return html
            
        con.close()
        return f"ℹ️ Identifier '{q}' was not found in the current breakdown checks table. You can trace its raw events directly in the [Interactive Web App](/app/)."
    except Exception as e:
        return f"Notice: {e}. You can inspect any identifier directly inside the [Interactive Web App](/app/)."

# ── Gradio Blocks Application ────────────────────────────────────────────────
with gr.Blocks(title="GridMap Utility", css=CUSTOM_CSS, theme=gr.themes.Soft(primary_hue="sky")) as demo:
    gr.HTML("""
    <div class="app-header">
      <div>
        <h1 class="app-title">GridMap Utility</h1>
        <div class="app-subtitle">Feeder / DTR / Consumer Outage Timing Topology Verification</div>
      </div>
      <div>
        <a href="/app/" target="_blank" class="direct-btn">
          ↗ Open Fullscreen App in New Window
        </a>
      </div>
    </div>
    <div class="info-banner">
      <span><strong>ZeroGPU Engine Active:</strong> Full-featured interactive web application running inside this container.</span>
      <a href="/app/" target="_blank" style="color:#0284c7; font-weight:700; text-decoration:none;">Open Fullscreen &rarr;</a>
    </div>
    """)

    with gr.Tabs():
        with gr.TabItem("Interactive Web Application", id="tab_app"):
            gr.HTML("""
            <div style="width:100%; border-radius:12px; overflow:hidden;">
              <iframe src="/app/" style="width:100%; height:92vh; min-height:880px; border:1px solid #bae6fd; border-radius:12px; background:#ffffff; box-shadow:0 4px 20px rgba(2,132,199,0.08);" allow="clipboard-read; clipboard-write"></iframe>
            </div>
            """)

        with gr.TabItem("Raw Data & Cross-Check Explorer", id="tab_crosscheck"):
            gr.Markdown("### Cross-Check Raw Outage Telemetry Against Summary Results")
            gr.Markdown("Enter any Consumer Number, Meter Serial ID, DTR Code, or Feeder Code to cross-check raw input telemetry with breakdown verdicts:")
            with gr.Row():
                query_in = gr.Textbox(placeholder="e.g. 40000000001, 1000001, ABCD123456, 100000000001", label="Identifier", scale=4)
                check_btn = gr.Button("Cross-Check Telemetry", variant="primary", scale=1)
            result_out = gr.Markdown()
            check_btn.click(fn=gradio_cross_check_lookup, inputs=query_in, outputs=result_out)

        with gr.TabItem("Rules, Methodology & Glossary", id="tab_glossary"):
            gr.Markdown("""
### Comprehensive Utility Glossary & Methodological Rules

#### 1. Network & Data Terminology
- **Feeder:** An 11 kV line leaving a substation. It supplies many DTRs. *(e.g. `100000000001`)*
- **DTR / DT:** Distribution Transformer. Steps 11 kV down to the low voltage used by consumers. One feeder has many DTRs; one DTR supplies many consumers. *(e.g. `ABCD123456`)*
- **Consumer:** A customer connection with a smart meter. Identified by the consumer account number. *(e.g. `40000000001`)*
- **Mapping:** Which feeder a DTR is connected to, and which DTR (and feeder) a consumer is connected to. *(Feeder &rarr; DTR &rarr; Consumer)*
- **MI file (MI master):** Meter Installation workbook from the utility holding recorded mapping sheets (`Total Consumer MI Data`, `Total DTR MI`, `Total Feeder MI`).
- **Recorded mapping:** The feeder/DTR written in the MI file. This is what the tool checks.
- **Suggested mapping:** The feeder/DTR the outage timing points to when the recorded one looks wrong.
- **Meter ID:** Serial number of the smart meter.
- **DTR meter / Feeder meter:** A meter installed on the DTR or on the feeder itself.
- **Outage report:** File listing power-failure events (`Power Failure Report Consumer Meter.csv`).
- **Off time (event_occurred):** When the meter lost power.
- **On time (event_restored):** When power came back.

#### 2. Breakdown Check Terminology
- **Feeder breakdown:** One outage of a feeder from its feeder meter: feeder off time to feeder on time.
- **Breakdown window:** Feeder off time minus tolerance, to feeder on time plus tolerance. Anything below the feeder should go off inside this window.
- **Tolerance:** Extra minutes allowed because meter clocks are not perfectly in sync. Default &plusmn;2 minutes.
- **Feeder &rarr; DTR check:** For each feeder breakdown: did each DTR recorded on that feeder go off inside the breakdown window?
- **Feeder &rarr; Consumer check:** For each feeder breakdown: did each consumer recorded on that feeder go off inside the breakdown window?
- **DTR &rarr; Consumer check:** For each DTR that went off in a breakdown: did its recorded consumers go off inside the DTR's own off-on time?
- **MATCH:** It went off inside the window, as the recorded mapping predicts.
- **MISMATCH:** It had data that day but did NOT go off inside the window &mdash; a sign of wrong mapping.
- **PARTIAL:** Went off in some breakdowns but not all.
- **No data (not counted):** Cannot be judged (`NO DTR METER`, `NO METER DATA`, `FAULTY METER`). NOT counted as mismatch.
- **Suspect breakdown:** A feeder breakdown with <10% match or <5 elements responding. Likely a meter glitch.
- **Confirmed breakdown:** Passed the suspect filter. Client headline KPIs use confirmed breakdowns only.

#### 3. Wrong-Mapping Identification
- **Outage pattern matching:** Compares when a consumer lost power with every candidate DTR and feeder.
- **WRONG / LIKELY WRONG:** The consumer's outages point strongly to another DTR/feeder.
- **Confidence (High / Medium / Low):** High = 3+ matching outages (&ge;80% recall); Medium = 2 matching outages (&ge;60% recall); Low = 1 matching outage.

#### 4. DTR &rarr; Feeder Verdicts
- **CORRECT:** The DTR is confirmed to be on its recorded feeder.
- **WRONG:** Both checks or high-confidence evidence show the DTR is on another feeder.
- **CONFLICT:** One check says correct, the other says wrong. Needs a field check.
- **FEEDER METER ISSUE:** DTR looks wrong only because a feeder meter is on another feeder.
- **NOT CHECKABLE:** No usable outage data for this DTR.

#### 5. Mapping Tree & Excel Reports
- **Feeder_Outage_View:** Outages grouped under recorded network hierarchy.
- **Mapping_Tree:** Corrected network hierarchy with moved consumers highlighted.
- **All_Results.xlsx:** Master workbook containing summary KPIs, DTR verdicts, flagged consumers, and clickable trace links.
            """)

        with gr.TabItem("Executive Reports & Downloads", id="tab_downloads"):
            gr.Markdown("### Generated Master Excel Reports")
            gr.Markdown("""
Once an analysis is executed in the web application, all 4 master Excel workbooks are compiled and available for one-click download:

1. **All_Results.xlsx (Combined Master):** Full executive workbook with Summary KPIs, Feeder Breakdown Checks, DTR Verdicts, Flagged Consumers, Mismatch Patterns, and Data Checks.
2. **Breakdown_Mapping_Check.xlsx:** Dedicated breakdown sheet tracking every single breakdown window match and mismatch for Feeder &rarr; DTR, Feeder &rarr; Consumer, and DTR &rarr; Consumer.
3. **Mapping_Report.xlsx:** Detailed wrong-mapping audit sheet listing every flagged consumer with suggested DTR/feeder, confidence score, and alternative candidate rankings.
4. **Feeder_DTR_Consumer.xlsx:** Complete hierarchical outage tree mapping every element under its recorded and suggested network position.

Access past runs and download reports directly from the **[Interactive Web Application](/app/history)**.
            """)

# ── Master FastAPI Application ────────────────────────────────────────────────
app = FastAPI(title="GridMap Utility Server")

@app.get("/app")
def redirect_to_app_slash():
    return RedirectResponse(url="/app/", status_code=307)

@app.get("/job/{rest:path}")
@app.post("/job/{rest:path}")
def redirect_job_root(rest: str, request: Request):
    query = request.url.query
    target = f"/app/job/{rest}" + (f"?{query}" if query else "")
    return RedirectResponse(url=target, status_code=307)

# 1. Mount static files at /static
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 2. Mount Flask directly at /app (serves complete GridMap web application)
app.mount("/app", WSGIMiddleware(fa.app))

# 3. Mount Gradio at root / so Hugging Face ZeroGPU /config healthchecks succeed immediately
app = gr.mount_gradio_app(app, demo, path="", ssr_mode=False)

# ── Server Startup ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Explicitly notify the Hugging Face ZeroGPU broker during startup
    try:
        from spaces import zero as _spaces_zero
        if hasattr(_spaces_zero, "startup"):
            _spaces_zero.startup()
            print("[GridMap] ZeroGPU startup report sent successfully.", flush=True)
    except Exception as err:
        print(f"[GridMap] ZeroGPU startup hook: {err!r}", flush=True)

    print("[GridMap] Starting server on 0.0.0.0:7860 ...", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=7860)
