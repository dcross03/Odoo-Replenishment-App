# Odoo Replenishment App

Demand-driven replenishment alerts for Sun Company, Inc. Replaces Odoo's
static min/max reordering rules with a sales-velocity-based model that
considers BOM component roll-up, vendor lead times, and a configurable
safety buffer.

## What it does

Every Wednesday at 6 AM MT, a scheduled GitHub Action:

1. Pulls trailing 52-week sales velocity per SKU — a gradual blend of Sage
   historical data and live Odoo sales data, weighted by weeks since cutover.
2. Pulls all BOMs and rolls parent demand into component demand.
3. Pulls every Buy-route storable product, its forecasted quantity,
   primary vendor, lead time, and default Purchase tab order quantity.
4. Categorizes each item:
   - **Urgent**: months of stock < lead time + 2-month buffer
   - **Add-on**: months of stock < lead time + 4-month buffer
   - **Low-stock catchall**: forecasted qty < 500 (caught regardless of velocity)
5. Sends a single consolidated digest email with per-vendor sections,
   each with its own signed "Create Draft PO" button.

A separate FastAPI service on Render handles the button clicks: verifies
the signed token, creates the draft PO in Odoo, redirects you to it.

## Architecture

```
GitHub Actions (cron: Wed 12:00 UTC)
    └─> scripts/run_replenishment.py
          ├─> Odoo XML-RPC (read-only)
          ├─> Sage history (data/sage/sage_sales_history.xlsx)
          └─> SendGrid (1 consolidated digest email)

Render (always-on FastAPI service)
    └─> webhook/main.py
          ├─> Verify signed token from email link
          └─> Odoo XML-RPC: create draft PO + redirect
```

## Replenishment formula

For each Buy-route SKU:

```
weekly_velocity = ((52 - N) * sage_weekly + N * odoo_weekly) / 52

where:
  N           = whole weeks since Odoo cutover, capped at 52
  sage_weekly = sage_12mo_total / 52
  odoo_weekly = odoo_units_sold_since_cutover / N

monthly_velocity = weekly_velocity * 52 / 12

total_monthly_demand = direct_monthly_velocity
                     + sum over all parent BOMs of:
                       (parent_monthly_velocity * bom_line_qty)

months_of_stock = virtual_available / total_monthly_demand
lead_time_months = vendor_lead_time_days / 30

urgent  if months_of_stock < lead_time_months + 2
add_on  if months_of_stock < lead_time_months + 4 (and not urgent)
```

## Exclusions

The following are silently excluded from alerts:
- Archived products (`active=False`)
- Non-storable products (`is_storable=False`) — services, fees, tooling
- Products tagged "Discontinued" (matched case-insensitively, also
  catches the typo "Discountinued" if it reappears)
- Items whose primary vendor is "Sun Company, Inc." or "Big Discoveries"
  (internal production — these should be on the Manufacture route)

## Project layout

```
odoo-replenishment-app/
├── app/
│   ├── config.py
│   ├── odoo/
│   │   ├── client.py             # XML-RPC client
│   │   └── queries.py            # Read operations
│   ├── services/
│   │   ├── sage.py               # Load Sage history
│   │   ├── velocity.py           # Direct + component demand roll-up
│   │   ├── alerts.py             # Categorization
│   │   └── email_builder.py      # Token signing + Jinja rendering + SendGrid
│   └── email_templates/
│       ├── base.css.inc.html     # Shared CSS (screen + print)
│       └── digest.html           # Single consolidated digest template
├── webhook/
│   ├── main.py                   # FastAPI app
│   ├── odoo_po.py                # Create draft PO in Odoo
│   └── requirements.txt
├── scripts/
│   ├── run_replenishment.py      # Main entry point (called by GitHub Action)
│   └── validate_skus.py          # One-off SKU cross-reference
├── data/sage/
│   └── sage_sales_history.xlsx   # Source-of-truth Sage data (committed)
├── .github/workflows/
│   └── weekly-replenishment.yml  # Wed 12:00 UTC
├── render.yaml                   # Render service definition
├── requirements.txt              # Main app deps
├── .env.production.template      # Env var template (copy to .env.production)
└── .gitignore
```

## Local development

```bash
python -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.production.template .env.production
# edit .env.production with real values

# Dry run: writes output/digest.html, no email sent
python scripts/run_replenishment.py --dry-run

# Validate Sage SKUs against Odoo
python scripts/validate_skus.py

# Override the "today" date for testing
python scripts/run_replenishment.py --dry-run --today 2026-08-15
```

## Deployment

### 1. Push to a new GitHub repo

```bash
cd odoo-replenishment-app
git init
git add .
git commit -m "Initial scaffold"
git remote add origin https://github.com/dcross03/odoo-replenishment-app.git
git push -u origin main
```

### 2. Configure GitHub secrets

In the repo: **Settings → Secrets and variables → Actions**

**Secrets** (encrypted):

| Name | Value |
|------|-------|
| `ODOO_URL` | `https://suncompany.odoo.com` |
| `ODOO_DB` | `suncompany` |
| `ODOO_USERNAME` | `dcross@suncompany.com` |
| `ODOO_API_KEY` | (your Odoo API key) |
| `SENDGRID_API_KEY` | (your SendGrid API key) |
| `EMAIL_FROM` | (verified SendGrid sender, e.g. `alerts@suncompany.com`) |
| `EMAIL_TO` | `dcross@suncompany.com` |
| `WEBHOOK_BASE_URL` | (URL of the Render service, after you deploy it) |
| `WEBHOOK_SHARED_SECRET` | (long random string — must match Render) |

**Variables** (non-secret, easier to update):

| Name | Value |
|------|-------|
| `ODOO_CUTOVER_DATE` | `2026-05-01` |
| `URGENT_BUFFER_MONTHS` | `2` |
| `ADDON_BUFFER_MONTHS` | `4` |
| `LOW_STOCK_THRESHOLD` | `500` |

### 3. Deploy webhook to Render

1. Sign in to Render and click **New → Web Service**.
2. Connect the GitHub repo.
3. Render will detect `render.yaml` and use it as the blueprint.
4. Set the env vars in the Render dashboard:
   - `ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY` — same as GitHub
   - `WEBHOOK_SHARED_SECRET` — **must match** the GitHub secret exactly
5. Deploy. Copy the URL (e.g. `https://odoo-replenishment-webhook.onrender.com`)
6. Back in GitHub, set the `WEBHOOK_BASE_URL` secret to that URL.

### 4. Trigger the first run

In the repo: **Actions → Weekly Replenishment Digest → Run workflow**

For a safe first run, set `dry_run` to `true`. The workflow writes the
digest HTML as an artifact you can download and inspect without sending.
After you've reviewed it, run again with `dry_run=false` to send the email.

After that, the cron will run automatically every Wednesday.

## Maintenance

### Updating the Sage history

The Sage history file (`data/sage/sage_sales_history.xlsx`) is committed
to the repo. To update it, replace the file and commit. The next run
picks it up. It naturally ages out: after 52 weeks post-cutover (May 2027),
the blend is 100% Odoo data and the Sage file is no longer used.

### Tuning thresholds

Edit the GitHub variables `URGENT_BUFFER_MONTHS`, `ADDON_BUFFER_MONTHS`,
`LOW_STOCK_THRESHOLD`. No code change needed.

### Re-running SKU validation

If you rename SKUs in Odoo and want to verify the Sage history still maps
correctly, run `python scripts/validate_skus.py` locally.
