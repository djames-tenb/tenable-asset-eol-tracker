# Tenable Asset EOL Tracker

A self-hosted web portal that syncs all assets from a Tenable VM / Tenable One tenant and shows their end-of-life (EOL) status, ranked by asset criticality — no spreadsheets, no external services, no API calls at page load.

Works entirely with Python's standard library: no `pip install`, no Docker, no external dependencies.

---

## What It Does

- **Full asset sync** — pulls every asset from Tenable VM via the Assets Export API (no 5 000-asset cap), scoped to VM scanner sources and assets last seen within 90 days; includes OS strings, installed-software CPE data, and asset tags
- **EOL correlation** — maps each OS and application to the corresponding [endoflife.date](https://endoflife.date) lifecycle data, then classifies every asset as **EOL**, **EOL Soon** (≤ 90 days), **Supported**, or **Unknown**
- **Local cache** — all Tenable data and EOL cycle data are stored in a local SQLite database; every page loads instantly without live API calls
- **Multi-tenant** — supports multiple Tenable tenants in a single instance; credentials are encrypted at rest
- **Criticality ranking** — every view surfaces Tenable's ACR so you can separate "EOL" from "EOL *and* business critical"
- **EOL trend** — one history point per sync, so you can show whether exposure is shrinking rather than just how bad it is today
- **Tag filtering** — filter any view by Tenable asset tags (environment, business unit, owner, or any custom category)

---

## Three Views

### EOL Overview Dashboard
The landing page. Shows end-of-life status across your entire estate with:
- **Summary cards** — EOL / EOL Soon / Supported / Unknown / Total, plus **Critical & EOL** (ACR ≥ 7 and EOL or EOL Soon). Each is clickable and drills into Asset Inventory with the matching filters pre-applied
- **EOL trend** — status counts over time, one point per completed sync, so you can show direction rather than just a snapshot
- **Upcoming EOLs** — OS versions expiring within the next 180 days, sorted by urgency with colour-coded countdowns
- **Top EOL OS Versions** — ranked list of OS versions with the most EOL or EOL-Soon assets, with split bar showing EOL vs EOL-Soon proportion
- **EOL by Tag** — stacked bar comparing EOL / EOL Soon / Supported / Unknown across tag values for any selected tag category
- **Status distribution donut** and **horizontal bar chart**
- **Tag filter** — all cards and charts scope to the selected tag

### Asset Inventory
Per-asset view, filterable by OS family, EOL status, ACR, and asset tags. The **Scope** selector switches the row status between *All*, *OS only*, and *Applications only* — OS-only reproduces a dedicated OS lifecycle view without a separate page. Sortable ACR column. Paginated (50 per page); search bypasses pagination and shows all matches. Click any row to open the **Drill-Down panel** showing:
- Identity attributes (hostnames, IPs, last seen, tenant)
- Scan metadata (ACR score, AES score, scanner type)
- Full EOL analysis — one row per matched product/cycle with status, EOL date, and endoflife.date reference
- **Why unknown** — per-CPE explanation of why a lifecycle match was not found (no vendor mapping, version outside the tracked range, suppressed rolling release, and so on)
- Tenable asset tags
- Raw installed-software CPE inventory

### Software Inventory
Server-side aggregated view of all detected software CPE strings. Groups by product and lists all detected versions with EOL status and asset counts. Expand a version to see the individual assets. Tick **Tracked only** to hide software with no endoflife.date mapping, which gives you a pure application-lifecycle view. Tag-filtered server-side, lazy-rendered for performance.

---

## Requirements

- Python 3.8 or later (no additional packages)
- Tenable VM or Tenable One API credentials (access key + secret key)

### Outbound network calls

Every destination this tool contacts, and why:

| Destination | Made by | Purpose |
|---|---|---|
| `cloud.tenable.com` (or your tenant URL) | Server | Asset export, tags, `/server/status` connectivity test. Carries your API keys |
| `endoflife.date` | Server | Product lifecycle data, cached 24 h |
| `cdnjs.cloudflare.com` | **Browser** | Loads `Chart.js 4.4.1` for the dashboard charts |

No other host is contacted, and no asset data ever leaves your machine — the
Tenable and endoflife.date calls are outbound requests for data, and
endoflife.date receives only a product slug in the URL path.

**Known limitation:** the Chart.js dependency is loaded from a CDN, so on an
air-gapped or egress-filtered host the charts will not render. Everything else
— tables, filters, drill-down, CSV export — works offline. To run fully
offline, download `chart.umd.min.js` into the repo and change the `<script
src>` in `templates/index.html` to a relative path.

Tenant URLs are restricted to `https://` on `cloud.tenable.com` or any
`*.tenable.com` host. Set `EOL_ALLOWED_HOSTS` to permit a private deployment.

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

All tenant configuration is stored in `config.json`. API credentials are obfuscated at rest: PBKDF2-derived keystream XORed with the plaintext, using a per-install key file (`.eol_portal_secret`, created on first run, chmod 600). Ciphertext carries an `enc:v1:` prefix. Plaintext keys you paste in are converted on the next save.

**Be clear about what this does and does not protect.** The key file sits beside `config.json` with the same owner and permissions, so anything able to read the ciphertext can also read the key. This defends against casual disclosure — a screenshot, a stray copy, an unencrypted backup — and **not** against a local attacker or malware running as your user. If you need real protection, keep the keys in the macOS Keychain or another OS secret store and load them into the environment, rather than relying on this.

**Never commit `config.json` or `.eol_portal_secret` to source control.** Both are in `.gitignore`.

Use a dedicated Tenable API key with the least privilege that works (asset export and tag read), and rotate it if the file is ever exposed.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5555` | Listening port |
| `EOL_BIND` | `127.0.0.1` | Bind address. The app has **no authentication** and holds Tenable API keys, so it listens on loopback only. Set `0.0.0.0` to expose it deliberately and restrict access at the network layer |
| `EOL_SOURCES` | `NESSUS_SCAN,NESSUS_AGENT` | Asset sources to analyse. ASM/EASM-discovered assets carry no OS or installed-software data, so they can never match a lifecycle entry and only inflate the Unknown count. Set `*` to keep every source |
| `EOL_INSECURE_TLS` | unset | Set to `1` to skip TLS certificate verification. Only for a Tenable instance behind a self-signed certificate — it exposes your API keys to interception |
| `EOL_ALLOWED_HOSTS` | `cloud.tenable.com,.tenable.com` | Hostnames a tenant URL may point at. A leading dot matches subdomains. Prevents the server being pointed at, say, a cloud metadata endpoint |
| `EOL_HOST_ALLOWLIST` | empty | Extra values accepted in the HTTP `Host` header. Requests arriving under any other name are refused, which blocks DNS rebinding. Add your LAN name here if you set `EOL_BIND=0.0.0.0` |

```bash
PORT=8080 EOL_BIND=0.0.0.0 python3 app.py
```

### Sync modes

`POST /api/jobs` accepts:

| Field | Effect |
|---|---|
| `tenant_id` | Required |
| `mode` | `full` (default) replaces every asset for the tenant. `delta` exports only assets whose `updated_at` moved since the last sync — much faster, but it cannot detect deleted assets, so run a full sync periodically to reconcile |
| `force_eol` | `true` re-downloads every endoflife.date feed, ignoring the 24-hour cache. Useful immediately before a demo |

---

## Troubleshooting

### `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`

A python.org framework build of Python on macOS ships with an **empty trust
store** and does not consult the macOS keychain, so every HTTPS request fails
until its bundled certificates are installed. The app detects an empty store
and falls back to `certifi` or a system CA bundle automatically, but if it
logs `No trusted CA certificates found`, fix the interpreter:

```bash
open "/Applications/Python 3.13/Install Certificates.command"   # match your version
```

or install `certifi` into whichever environment runs the app:

```bash
python3 -m pip install certifi
```

Confirm the store is populated:

```bash
python3 -c "import ssl; print(len(ssl.create_default_context().get_ca_certs()), 'CA certs')"
```

Anything other than `0` is fine. `EOL_INSECURE_TLS=1` will also silence the
error, but it sends your Tenable API keys over an unverified channel — use it
only for a Tenable instance behind a self-signed certificate.

### Everything shows as "Unknown"

Almost always the asset source mix rather than the matching logic. ASM/EASM
assets carry no OS or installed-software data, so they cannot be correlated.
The default `EOL_SOURCES=NESSUS_SCAN,NESSUS_AGENT` excludes them; if you have
overridden it, that is the first thing to check. For an individual asset, open
its drill-down and expand **Why unknown** for a per-CPE explanation.

---

## How It Works

```
Tenable VM
   │  Assets Export API (POST /assets/export)
   │  → asset id, hostnames, IPs, OS strings, installed_software CPEs
   │  → asset tags, ACR / AES scores, sources
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
