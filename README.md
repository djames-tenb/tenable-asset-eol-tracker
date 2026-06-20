# Tenable Asset EOL Tracker

A self-hosted web portal that syncs all assets from a Tenable VM / Tenable One tenant and shows their end-of-life (EOL) status across five purpose-built views — no spreadsheets, no external services, no API calls at page load.

Works entirely with Python's standard library: no `pip install`, no Docker, no external dependencies.

---

## What It Does

- **Full asset sync** — pulls every asset from Tenable VM via the Assets Export API (no 5 000-asset cap), scoped to VM scanner sources and assets last seen within 90 days; includes OS strings, installed-software CPE data, and asset tags
- **EOL correlation** — maps each OS and application to the corresponding [endoflife.date](https://endoflife.date) lifecycle data, then classifies every asset as **EOL**, **EOL Soon** (≤ 90 days), **Supported**, or **Unknown**
- **Local cache** — all Tenable data and EOL cycle data are stored in a local SQLite database; every page loads instantly without live API calls
- **Multi-tenant** — supports multiple Tenable tenants in a single instance; credentials are encrypted at rest
- **Tag filtering** — filter any view by Tenable asset tags (environment, business unit, owner, or any custom category) — available on all five pages

---

## Five Views

### OS EOL Overview Dashboard
The landing page. Shows OS end-of-life status across your entire estate with:
- **Summary cards** — EOL / EOL Soon / Supported / Unknown / Total asset counts, each clickable to drill directly to the OS EOL page with the matching status filter pre-applied
- **Upcoming EOLs** — OS versions expiring within the next 180 days, sorted by urgency with colour-coded countdowns
- **Top EOL OS Versions** — ranked list of OS versions with the most EOL or EOL-Soon assets, with split bar showing EOL vs EOL-Soon proportion
- **EOL by Tag** — stacked bar comparing EOL / EOL Soon / Supported / Unknown across tag values for any selected tag category
- **Status distribution donut** and **horizontal bar chart**
- **Tag filter** — all cards and charts scope to the selected tag

### Asset Inventory
Filterable list of all synced assets. Filter by OS family, EOL status, and asset tags. Paginated (50 per page); search bypasses pagination and shows all matches. Click any row to open the **Drill-Down panel** showing:
- Identity attributes (hostnames, IPs, last seen, tenant)
- Scan metadata (ACR score, AES score, scanner type)
- Full EOL analysis — one row per matched product/cycle with status, EOL date, and endoflife.date reference
- Tenable asset tags
- Raw installed-software CPE inventory

### OS EOL
Per-asset view of operating system end-of-life status. Filter by OS family, EOL status, and asset tags. Paginated (50 per page) with full-text search.

### App EOL
Grouped by unique (product, version) pair across the entire estate. Expand any row to see which assets are running that version. Filter by EOL status and asset tags. Paginated (50 per page) with full-text search.

### Software Inventory
Server-side aggregated view of all detected software CPE strings. Groups by product and lists all detected versions with asset counts. Expand a version to see the individual assets. Tag-filtered server-side, lazy-rendered for performance.

---

## Requirements

- Python 3.8 or later (no additional packages)
- Tenable VM or Tenable One API credentials (access key + secret key)
- Network access to `cloud.tenable.com` and `endoflife.date`

---

## Quick Start

```bash
git clone https://github.com/djames-tenb/tenable-asset-eol-tracker.git
cd tenable-asset-eol-tracker/eol-portal
python3 app.py
```

Open your browser to **http://localhost:5555**.

On first run the app has no tenants configured. Click **Tenants** in the sidebar, then **+ Add Tenant** and enter:

| Field | Value |
|---|---|
| Tenant name | Any label (e.g., "Production") |
| URL | `https://cloud.tenable.com` |
| Access key | Your Tenable API access key |
| Secret key | Your Tenable API secret key |

Then click **Sync** to fetch all assets. Sync time depends on asset count; 30 000 assets typically takes 3–5 minutes.

---

## Configuration

All tenant configuration is stored in `config.json`. API credentials are encrypted at rest using a per-install key file (`.eol_portal_secret`, created automatically on first run, chmod 600).

**Never commit `config.json` or `.eol_portal_secret` to source control.** Both are in `.gitignore`.

To change the listening port:

```bash
PORT=8080 python3 app.py
```

---

## How It Works

```
Tenable VM
   │  Assets Export API (POST /assets/export)
   │  → asset id, hostnames, IPs, OS strings, installed_software CPEs
   │  → asset tags (Tags API)
   ↓
app.py (Python stdlib ThreadingHTTPServer — zero dependencies)
   │  Parses OS strings      → endoflife.date product/cycle
   │  Parses CPE 2.3/2.2    → endoflife.date product/cycle
   │  Fetches cycle data (eol date, lts, latest release)
   │  Computes status: eol / eol_soon / supported / unknown
   │  /api/software/inventory → server-side CPE aggregation
   ↓
SQLite (eol_data.db)
   │  assets      — one row per asset (EOL entries + software as JSON)
   │  eol_cycles  — persisted endoflife.date cache (survives restarts)
   │  sync_state  — last sync timestamps per tenant
   ↓
Browser dashboard (single-page, vanilla JS — no build step)
   ├── OS EOL Overview  — clickable summary cards + 3 analytical widgets
   ├── Asset Inventory  — filterable + drillable per-asset explorer
   ├── OS EOL          — per-asset OS lifecycle status
   ├── App EOL         — grouped by product/version across estate
   └── Software Inventory — server-aggregated CPE view with lazy expand
```

### EOL Matching

OS strings (e.g., `"Red Hat Enterprise Linux 8.6"`) are matched against a curated set of regex patterns to identify the endoflife.date product slug and cycle.

CPE strings (e.g., `"cpe:2.3:a:apache:log4j:2.14.1:*:*:*:*:*:*:*"`) are matched against an internal `CPE_MAP` that maps `vendor:product` pairs to endoflife.date slugs. Version extraction uses per-product regex patterns to handle quirks (build numbers, CalVer, service-pack suffixes, etc.).

Matching runs in four passes of decreasing specificity: exact → prefix → qualifier-stripped → reverse prefix.

---

## Supported Products (EOL Tracking)

**Operating Systems**: Windows (desktop + server), RHEL, CentOS (stream + legacy), Fedora, AlmaLinux, Rocky Linux, Oracle Linux, Debian, Ubuntu, SLES, openSUSE, Amazon Linux, Alpine Linux, macOS, FreeBSD, Oracle Solaris

**Applications** (selection): OpenSSL, Node.js, Python, PHP, Ruby, Java (Temurin/Adoptium), .NET / .NET Framework, Spring Boot, Apache HTTP Server, nginx, Tomcat, MySQL, PostgreSQL, SQLite, MongoDB, Redis, Elasticsearch, Docker Engine, Kubernetes, VMware ESXi, vCenter, SQL Server, and 60+ more via CPE mapping

---

## Generating API Credentials

1. Log in to Tenable VM / Tenable One
2. Go to **Settings → My Account → API Keys**
3. Click **Generate** and copy both keys

The API user needs the **Basic** user role. No administrator access is required.

---

## Running as a Service (macOS launchd)

Create `~/Library/LaunchAgents/com.tenable.eol-portal.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tenable.eol-portal</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/eol-portal/app.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/eol-portal</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tenable.eol-portal.plist
```

---

## Reprocessing EOL Data

After updating `app.py` (e.g., adding new CPE mappings), recompute all EOL entries without a full Tenable sync:

```bash
python3 reprocess_eol.py
```

This fetches fresh endoflife.date data and re-evaluates every asset already in the database.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Main HTTP server, all backend logic, EOL matching engine |
| `templates/index.html` | Single-page frontend (vanilla JS, no build step) |
| `reprocess_eol.py` | Standalone EOL reprocessor (run after adding new CPE mappings) |
| `eol_data.db` | SQLite database (auto-created, gitignored) |
| `config.json` | Tenant configuration with encrypted credentials (gitignored) |
| `.eol_portal_secret` | Per-install encryption key (auto-created, gitignored, chmod 600) |

---

## License

MIT — see [LICENSE](LICENSE)
