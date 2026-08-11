---
name: "Tenable Asset EOL Tracker"
author: "djames-tenb"
github_url: "https://github.com/djames-tenb/tenable-asset-eol-tracker"
description: "Self-hosted web portal that correlates Tenable VM assets with endoflife.date, ranks EOL exposure by asset criticality (ACR), and tracks whether that exposure is shrinking over time — zero dependencies, instant load, encrypted credentials"
license: "MIT"
type: "tool"
tier: "unreviewed"
tags: ["eol", "vulnerability-management", "asset-management", "end-of-life", "tenable-vm", "python", "endoflife-date", "lifecycle", "patch-management", "dashboard", "software-inventory", "cpe", "os-lifecycle", "security-operations"]
framework: "Python + SQLite"
integrations: ["Tenable"]
date_added: 2026-06-20
---

A self-hosted web dashboard that pulls all assets from a Tenable VM or Tenable One tenant and correlates them with lifecycle data from [endoflife.date](https://endoflife.date), giving security teams instant visibility into which assets are running end-of-life operating systems or applications.

## What it does

- Syncs all assets from one or more Tenable VM / Tenable One tenants using the Assets Export API (no 5 000-asset cap) — scoped to VM scanner sources only, assets last seen within 90 days, including OS strings, installed-software CPE data, and asset tags
- Maps each OS and application to the corresponding endoflife.date product lifecycle entry and classifies assets as **EOL**, **EOL Soon** (within 90 days), **Supported**, or **Unknown**
- Ranks EOL exposure by Tenable's **Asset Criticality Rating**, so "EOL" can be separated from "EOL *and* business critical"
- Records one history point per sync and charts the **EOL trend**, showing whether exposure is shrinking rather than only how bad it is today
- Caches all Tenable data and EOL cycle data in a local SQLite database so every page loads instantly with no live API calls
- Supports multiple tenants in a single instance; credentials are obfuscated at rest (PBKDF2-derived keystream XOR, per-install key file). The README is explicit that this protects against casual disclosure rather than a local attacker, since the key file sits alongside the ciphertext
- Binds to loopback with no wildcard CORS, validates the HTTP `Host` header to block DNS rebinding, verifies TLS on every outbound call, and restricts tenant URLs to HTTPS on allowlisted Tenable hosts
- Provides **three views**, all filterable by asset tag: an EOL dashboard, an Asset Inventory with an OS/application scope selector and per-asset drill-down, and a Software Inventory with a tracked-only filter
- Explains its own gaps: the drill-down shows, per CPE, exactly why a lifecycle match was not found
- Light and dark themes, following the OS setting with a manual override

## How it works

The app is a single Python file (`app.py`) that runs a `ThreadingHTTPServer` — no framework, no external libraries, no pip install. On **Sync**, it:

1. Calls Tenable's `POST /assets/export` API to download all assets in parallel chunks, then scopes the result to VM scanner sources (`NESSUS_SCAN`, `NESSUS_AGENT`) and the last 90 days
2. Reads asset tags from the export payload and attaches them to each asset for filtering
3. Refreshes lifecycle data from endoflife.date for every mapped OS and application product, respecting a 24-hour cache
4. Parses OS strings and CPE 2.3/2.2 strings using a curated mapping of 100+ vendor:product entries, with per-product version normalizers to handle quirks (build numbers, CalVer dates, service-pack suffixes)
5. Runs a four-pass matching algorithm (exact → prefix → qualifier-stripped → reverse prefix) against endoflife.date cycle data, always preferring the most specific cycle
6. Persists everything to SQLite — surviving restarts with no re-fetch needed — and appends one history point per sync

EOL status and countdowns are re-derived from the stored lifecycle date on every read, so a dashboard served from a month-old sync still shows accurate days-remaining rather than the numbers frozen at sync time.

The frontend is a single-page `index.html` with vanilla JavaScript — no build step, no Node.js.

## Dashboard highlights

The **EOL Overview** dashboard gives security teams a set of analytical widgets beyond the basic summary counts:

- **Critical & EOL** — assets with an ACR of 7 or higher that are already EOL or expiring within 90 days. The one number that answers "what do I fix first"
- **EOL trend** — status counts over time from the sync history, so improvement is demonstrable
- **Upcoming EOLs** — OS versions expiring within 180 days, sorted by urgency with colour-coded countdowns (red ≤ 30 days, orange ≤ 90 days)
- **Top EOL OS Versions** — ranked list of the OS versions with the most EOL / EOL-Soon assets, with a split bar showing proportion
- **EOL by Tag** — stacked bar comparing EOL status across tag values for any selected tag category (e.g., Production vs Dev vs Staging)

Each summary card is clickable and navigates to the Asset Inventory with the matching scope, status and tag filter pre-applied.

## Asset drill-down

Clicking any row in the Asset Inventory opens a full detail panel showing identity attributes, scan metadata, ACR and AES risk scores, the complete EOL analysis (one row per matched product/cycle), Tenable asset tags, and the raw CPE software inventory.
