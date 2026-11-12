---
title: GridMap Feeder DTR Consumer Mapping Check
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: "5.9.1"
app_file: app.py
pinned: false
license: mit
short_description: Electricity utility mapping check tool
---

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
   - **Light &amp; Dark Mode:** Auto/Light/Dark toggle persisted per browser.

3. **4 Comprehensive Excel Downloads**

4. **Privacy &amp; Data Security:**
   - Zero Login: isolated private workspace via cookie
   - Automatic Cleanup after 7 days
   - Consumer personal data stripped from all views
