"""Main entry point for the weekly replenishment run.

Local dry run (writes the digest HTML to ./output/, no email sent):
    python scripts/run_replenishment.py --dry-run

Live run (sends one consolidated digest email via SendGrid):
    python scripts/run_replenishment.py

The GitHub Action calls this without --dry-run on Wednesday mornings.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.odoo.client import OdooClient
from app.odoo.queries import (
    fetch_all_boms,
    fetch_buy_route_products,
    fetch_sales_since,
)
from app.services.alerts import categorize, group_by_vendor, Tier
from app.services.email_builder import render_digest, send_email
from app.services.sage import load_sage_weekly_velocity
from app.services.velocity import (
    compute_velocities_with_parents,
    weeks_since_cutover,
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def fetch_parent_template_skus(client: OdooClient, template_ids: list[int]) -> dict[int, str]:
    if not template_ids:
        return {}
    raw = client.search_read(
        "product.template",
        domain=[("id", "in", template_ids)],
        fields=["id", "default_code"],
    )
    return {
        r["id"]: (r.get("default_code") or "").strip()
        for r in raw
        if r.get("default_code")
    }


def run(dry_run: bool, today: date) -> int:
    setup_logging()
    log = logging.getLogger("run")

    config = Config.load()
    client = OdooClient.from_config(config)

    log.info("Fetching data from Odoo...")
    products = fetch_buy_route_products(client)
    boms = fetch_all_boms(client)
    sales = fetch_sales_since(client, config.odoo_cutover_date)
    sage_weekly = load_sage_weekly_velocity()

    template_ids = list({b.parent_template_id for b in boms})
    parent_skus = fetch_parent_template_skus(client, template_ids)

    n_weeks = weeks_since_cutover(today, config.odoo_cutover_date)
    log.info("N weeks since cutover: %d (today=%s)", n_weeks, today)

    velocities = compute_velocities_with_parents(
        products=products,
        sage_weekly_by_sku=sage_weekly,
        odoo_sale_lines=sales,
        boms=boms,
        parent_skus_by_template=parent_skus,
        today=today,
        cutover=config.odoo_cutover_date,
    )

    rows, internal = categorize(
        products=products,
        velocities=velocities,
        urgent_buffer_months=config.urgent_buffer_months,
        addon_buffer_months=config.addon_buffer_months,
        low_stock_threshold=config.low_stock_threshold,
    )

    digests, orphan_low_stock, unassigned = group_by_vendor(rows)

    urgent_count = sum(1 for r in rows if r.tier == Tier.URGENT)
    addon_count = sum(1 for r in rows if r.tier == Tier.ADDON)
    critical_count = sum(1 for r in rows if r.is_critical and r.tier == Tier.URGENT)
    log.info(
        "Vendors=%d urgent=%d (critical=%d) addon=%d orphan_low_stock=%d unassigned=%d",
        len(digests), urgent_count, critical_count, addon_count,
        len(orphan_low_stock), len(unassigned),
    )

    log.info("Rendering digest...")
    html = render_digest(
        config=config,
        run_date=today,
        n_weeks=n_weeks,
        digests=digests,
        orphan_low_stock=orphan_low_stock,
        unassigned=unassigned,
    )

    subject = f"[Replenishment] Weekly Digest — {today.isoformat()}"
    if critical_count:
        subject = f"[Replenishment] {critical_count} BACKORDERED — Weekly Digest — {today.isoformat()}"

    if dry_run:
        out_dir = Path(__file__).resolve().parent.parent / "output"
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "digest.html"
        out_path.write_text(html, encoding="utf-8")
        log.info("Dry run: wrote digest to %s", out_path)
        log.info("Would send with subject: %s", subject)
        return 0

    ok = send_email(config, subject, html)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Render digest to ./output/ instead of emailing.")
    parser.add_argument("--today", default=None,
                        help="Override 'today' date (YYYY-MM-DD). For testing.")
    args = parser.parse_args()
    today = date.fromisoformat(args.today) if args.today else date.today()
    return run(dry_run=args.dry_run, today=today)


if __name__ == "__main__":
    raise SystemExit(main())
