#!/usr/bin/env python3
"""
reprocess_eol.py — Recompute all EOL entries in eol_data.db using the
                   current app.py logic WITHOUT a Tenable API sync.

Run once after code changes to app.py to apply fixes immediately:
    python3 reprocess_eol.py

What it does:
  1. Loads all functions from app.py (parse_os_eol, parse_cpe_eol, etc.)
  2. Fetches fresh EOL cycle data from endoflife.date (all mapped products)
  3. Re-evaluates every asset row using the fixed matching code
  4. Writes updated eol_entries + overall_status back to the database
  5. Prints a before/after summary of unknown counts
"""

import os, sys, json, sqlite3, logging, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_PY   = os.path.join(BASE_DIR, "app.py")
DB_FILE  = os.path.join(BASE_DIR, "eol_data.db")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reprocess")

# ── Load app.py into a controlled namespace ──────────────────────────────────
log.info("Loading app.py …")
with open(APP_PY) as f:
    src = f.read()

ns = {"__file__": APP_PY, "__name__": "__reprocess__"}
# Suppress the server from starting
exec(compile(src.replace('if __name__ == "__main__":', 'if False:'),
             APP_PY, "exec"), ns)

refresh_eol_cache = ns["refresh_eol_cache"]
parse_os_eol      = ns["parse_os_eol"]
parse_cpe_eol     = ns["parse_cpe_eol"]
EOL_CACHE         = ns["EOL_CACHE"]
EOL_CACHE_TS      = ns["EOL_CACHE_TS"]

DISPLAY_PRI = {"eol": 0, "eol_soon": 1, "supported": 2, "unknown": 3}

# ── Step 1: fetch fresh EOL data ─────────────────────────────────────────────
log.info("Fetching EOL cycle data from endoflife.date …")
refresh_eol_cache(force=True)
total_products = len(EOL_CACHE)
tracked = sum(1 for v in EOL_CACHE.values() if v)
log.info(f"Cache: {total_products} products fetched, {tracked} with cycle data, "
         f"{total_products - tracked} not tracked (will be suppressed)")

# ── Step 2: connect to DB and enumerate assets ───────────────────────────────
con = sqlite3.connect(DB_FILE)
con.row_factory = sqlite3.Row

rows = con.execute(
    "SELECT id, tenant_id, os, software, eol_entries FROM assets"
).fetchall()
log.info(f"Loaded {len(rows)} assets from database")

# ── Step 3: recompute ─────────────────────────────────────────────────────────
before_unk = after_unk = before_entries = after_entries = 0
updates = []

for row in rows:
    asset_id  = row["id"]
    tenant_id = row["tenant_id"]
    os_str    = row["os"] or ""
    software  = json.loads(row["software"] or "[]")
    old_entries = json.loads(row["eol_entries"] or "[]")

    before_unk     += sum(1 for e in old_entries if e.get("status") == "unknown")
    before_entries += len(old_entries)

    new_entries = []

    # OS entry
    if os_str:
        info = parse_os_eol(os_str.lower())
        if info:
            info["type"] = "Operating System"
            new_entries.append(info)

    # Application entries from CPE strings
    seen_cpes: set = set()
    for cpe_str in software:
        if not isinstance(cpe_str, str) or not cpe_str:
            continue
        if cpe_str in seen_cpes:
            continue
        seen_cpes.add(cpe_str)

        # Skip OS-type CPEs (prefix o:)
        try:
            prefix = cpe_str[8:] if cpe_str.startswith("cpe:2.3:") else cpe_str[5:]
            if prefix.startswith("o:"):
                continue
        except Exception:
            continue

        info = parse_cpe_eol(cpe_str)
        if info:
            # Import human-name function from app ns
            info["name"]    = ns["_cpe_human_name"](cpe_str)
            info["cpe_raw"] = cpe_str
            info["type"]    = "Application"
            new_entries.append(info)
        # else: unmapped CPE — silently skip (not tracked)

    # Sort: OS first, then apps by urgency
    os_ents  = [e for e in new_entries if e.get("type") == "Operating System"]
    app_ents = [e for e in new_entries if e.get("type") != "Operating System"]
    app_ents.sort(key=lambda x: (DISPLAY_PRI.get(x.get("status"), 3),
                                  (x.get("name") or "").lower()))
    new_entries = os_ents + app_ents

    worst = min(new_entries,
                key=lambda x: DISPLAY_PRI.get(x.get("status"), 3),
                default=None)
    overall = worst["status"] if worst else "unknown"

    after_unk     += sum(1 for e in new_entries if e.get("status") == "unknown")
    after_entries += len(new_entries)

    updates.append((json.dumps(new_entries), overall, asset_id, tenant_id))

# ── Step 4: write to DB ───────────────────────────────────────────────────────
log.info(f"Writing {len(updates)} updated rows …")
con.executemany(
    "UPDATE assets SET eol_entries=?, overall_status=? WHERE id=? AND tenant_id=?",
    updates
)
con.commit()
con.close()

# ── Step 5: summary ───────────────────────────────────────────────────────────
print()
print("═" * 60)
print("  Reprocessing complete")
print("═" * 60)
print(f"  Assets processed     : {len(rows):>6,}")
print(f"  EOL entries before   : {before_entries:>6,}")
print(f"  EOL entries after    : {after_entries:>6,}")
print(f"  Unknown entries before: {before_unk:>5,}")
print(f"  Unknown entries after : {after_unk:>5,}")
reduction = before_unk - after_unk
if before_unk:
    pct = 100 * reduction / before_unk
    print(f"  Unknowns resolved    : {reduction:>5,}  ({pct:.0f}% reduction)")
print("═" * 60)
print()
print("Restart the app and refresh the browser to see updated data.")
