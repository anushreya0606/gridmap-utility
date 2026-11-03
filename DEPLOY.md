# Deployment Guide: Grid Mapping Check Website

This website checks Feeder–DTR–Consumer mapping for an electricity utility using outage telemetry.
The analysis requires **at least 3–4 GB of RAM** during the matching stage for a full circle's data.

---

## Important Notice on Free Hosts (Render Free & Vercel)

* **Render Free (512 MB RAM):** **Will crash with Out-Of-Memory (OOM / Exit 137).** 512 MB is insufficient to process 60+ MB CSV and 37 MB Excel files in memory. Render also has no persistent disk on free tiers, meaning all runs and stored events are lost on every restart.
* **Vercel:** Vercel is designed for serverless functions with a **4.5 MB maximum upload payload**. The consumer outage CSV is 60+ MB, so uploads will be rejected immediately with `413 Payload Too Large`. Serverless functions also time out after 10–60 seconds, whereas this analysis takes ~2–3 minutes.

---

## Option 1: Instant 1-Minute Public HTTPS Website (100% Free, Zero Signup)

If you need a public URL immediately that **never crashes and costs $0**:

1. Double-click **`start_public_website.bat`**.
2. It automatically launches the Python engine and creates a secure Cloudflare Quick Tunnel.
3. In 5–10 seconds, it prints your live public URL:
   ```
   https://random-name.trycloudflare.com
   ```
4. Share this link with anyone. 
   - **RAM:** Uses your computer's 16 GB RAM (zero risk of out-of-memory).
   - **Cost:** Free forever, no account, no credit card.
   - **Privacy:** `GRIDMAP_PUBLIC=1` gives every visitor their own private workspace via cookie.

---

## Option 2: Hugging Face Spaces (Free Cloud 16 GB RAM)

Hugging Face Spaces offers a **16 GB RAM CPU Basic** environment for free:

1. Create a free account at [huggingface.co](https://huggingface.co).
2. Click **New Space** &rarr; give it a name &rarr; select **Docker** (or **Gradio**).
3. Set Space hardware to **CPU Basic (2 vCPU, 16 GB RAM - Free)**.
4. Upload all files from this directory (excluding `data/`).
5. Hugging Face builds the Docker container automatically and gives you a link:
   ```
   https://YOUR_USERNAME-YOUR_SPACE.hf.space
   ```
*Note: Without paid persistent disk ($5/mo), the Space resets stored events if it goes to sleep due to inactivity.*

---

## Option 3: Render (Paid 4 GB Tier)

If you choose Render, you **must select a plan with at least 4 GB RAM**:

1. Push this repository to GitHub or GitLab.
2. In [Render Dashboard](https://dashboard.render.com), click **New +** &rarr; **Web Service**.
3. Connect your repository.
4. Select **Docker** as the environment.
5. In **Instance Type**, select **Standard (4 GB RAM, 2 vCPU)** or higher. **Do NOT select Free (512 MB).**
6. Under **Disks**, add a Persistent Disk:
   - **Mount Path:** `/data`
   - **Size:** 10 GB or higher
7. Set Environment Variables:
   - `GRIDMAP_DATA` = `/data`
   - `GRIDMAP_PUBLIC` = `1`
   - `GRIDMAP_KEEP_DAYS` = `7`
8. Deploy.

---

## Option 4: On-Premise Utility Server or Private VM (Recommended for Utilities)

Because outage reports contain utility operational data, deploying on an internal VM or office server behind the utility firewall is the safest choice:

### With Docker:
```bash
docker build -t gridmap .
docker run -d --name gridmap -p 8000:8000 -v /opt/gridmap-data:/data --restart unless-stopped gridmap
```

### Without Docker (Windows Server or Ubuntu Linux):
```bash
pip install -r requirements.txt
set GRIDMAP_DATA=D:\gridmap-data
python app.py --host 0.0.0.0 --port 8000
```

---

## Privacy & Security Architecture

1. **No Login Required:** In public mode (`GRIDMAP_PUBLIC=1`), visitors are isolated into private workspaces using encrypted session cookies (`wid`). Visitors cannot see each other's data.
2. **Workspaces Purged:** Inactive workspaces and files are permanently deleted after `GRIDMAP_KEEP_DAYS` (default 7 days).
3. **Consumer Privacy:** Consumer names and street addresses are permanently stripped from web dashboard views and Excel downloads.
4. **Single-Worker Execution:** Run only one server process/worker because background job progress and memory locking (`RUN_LOCK`) are held in process memory.
