[README.md](https://github.com/user-attachments/files/30508496/README.md)
# Shipping Smart Vessel Traffic — auto-updating dashboard

This folder turns the dashboard into one that refreshes itself automatically,
without needing anyone to click "refresh" or ask Claude to re-pull data.

**How it works:**
1. `scraper.py` fetches TPS Surabaya & Teluk Lamong's public schedule pages and
   writes `data/vessel_data.json`.
2. `.github/workflows/update-data.yml` runs that script automatically every
   ~15 minutes, on GitHub's own servers (no CORS issue there — it's a plain
   server-to-server request, not a browser one).
3. `index.html` (the dashboard) fetches `data/vessel_data.json` on load and
   every 5 minutes after that. If the file isn't reachable yet, it falls back
   to a static snapshot and says so clearly (yellow "STATIC SNAPSHOT" badge).

Once set up, you never touch anything again — GitHub does the work.

---

## Setup (about 10 minutes, all free)

### 1. Create a GitHub account (if you don't have one)
https://github.com/signup

### 2. Create a new repository
- Go to https://github.com/new
- Name it anything, e.g. `vessel-dashboard`
- Set it to **Public** (required for free GitHub Pages)
- Click **Create repository**

### 3. Upload these files
In your new repo, click **Add file → Upload files**, and drag in this entire
folder's contents, keeping the structure:
```
vessel-dashboard/
├── .github/workflows/update-data.yml
├── data/vessel_data.json
├── index.html
├── requirements.txt
├── scraper.py
└── README.md
```
Commit the upload.

### 4. Allow Actions to push updates
- Go to **Settings → Actions → General**
- Under "Workflow permissions", select **Read and write permissions**
- Click **Save**
(Without this, the workflow can fetch data but can't commit the updated file.)

### 5. Turn on GitHub Pages
- Go to **Settings → Pages**
- Under "Build and deployment" → Source: **Deploy from a branch**
- Branch: **main**, folder: **/ (root)**
- Click **Save**
- GitHub will give you a URL like:
  `https://<your-username>.github.io/vessel-dashboard/`
  (takes 1-2 minutes to go live the first time)

### 6. Run the workflow once manually (don't wait 15 minutes)
- Go to the **Actions** tab in your repo
- Click **Update vessel data** on the left
- Click **Run workflow** → **Run workflow**
- Wait ~30 seconds, refresh the page — you should see a green checkmark

### 7. Open your dashboard
Visit `https://<your-username>.github.io/vessel-dashboard/`
You should see a green **🟢 LIVE (auto-refreshing)** badge once step 6 has
completed successfully at least once.

---

## Checking if something's wrong

- **Still shows "🟡 STATIC SNAPSHOT"** → the workflow hasn't successfully run
  yet, or GitHub Pages hasn't finished deploying. Check the Actions tab for
  errors (click the failed run to see logs).
- **Workflow fails on the "Run scraper" step** → the source sites may have
  changed their page structure since this was built. Send me the error log
  from the Actions tab and I'll fix `scraper.py`.
- **Workflow fails on "Commit updated data"** → double check step 4
  (Read and write permissions).

## Changing the refresh interval
Edit the `cron` line in `.github/workflows/update-data.yml`.
`*/15 * * * *` = every 15 minutes. GitHub's free tier doesn't guarantee
exact timing under load, and won't run more often than every 5 minutes
in practice — `*/15` is a safe, reliable choice.
