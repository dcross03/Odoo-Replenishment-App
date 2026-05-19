"""Demand calculation engine.

Inputs:
    - Sage weekly velocity per Odoo SKU (from sage.py)
    - Confirmed Odoo sale lines since the cutover date (from queries.py)
    - All Odoo BOMs and their lines (from queries.py)
    - All buy-route products with their template ids

Output:
    - For each product, total monthly demand =
        direct_monthly_velocity + sum of (parent_monthly_velocity * bom_qty)
        across all BOMs that consume this product as a component.

The "direct" velocity is the gradual blend:
    weekly_velocity = ((52 - N) * sage_weekly + N * odoo_weekly) / 52
where N is the number of weeks since Odoo cutover, capped at 52.

monthly_velocity = weekly_velocity * 52 / 12
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.odoo.queries import Bom, Product, SaleLine

logger = logging.getLogger(__name__)

WEEKS_PER_YEAR = 52.0
MONTHS_PER_YEAR = 12.0


@dataclass
class Velocity:
    """Per-product velocity, broken out so we can audit the components."""
    sage_weekly: float = 0.0
    odoo_weekly: float = 0.0
    blended_weekly: float = 0.0       # the final blended number for this product
    monthly_direct: float = 0.0       # blended_weekly * 52/12
    monthly_component_rollup: float = 0.0  # from BOMs that use this as a component
    monthly_total: float = 0.0        # direct + rollup

    @property
    def has_velocity(self) -> bool:
        return self.monthly_total > 0


def weeks_since_cutover(today: date, cutover: date) -> int:
    """Whole weeks since cutover, floored at 0, capped at 52."""
    delta_days = (today - cutover).days
    if delta_days <= 0:
        return 0
    weeks = delta_days // 7
    return min(weeks, int(WEEKS_PER_YEAR))


def _blend(sage_weekly: float, odoo_weekly: float, weeks_in: int) -> float:
    """Gradual-blend formula: ((52 - N) * sage + N * odoo) / 52."""
    n = max(0, min(int(weeks_in), int(WEEKS_PER_YEAR)))
    return ((WEEKS_PER_YEAR - n) * sage_weekly + n * odoo_weekly) / WEEKS_PER_YEAR


def compute_velocities(
    products: list[Product],
    sage_weekly_by_sku: dict[str, float],
    odoo_sale_lines: Iterable[SaleLine],
    boms: list[Bom],
    today: date,
    cutover: date,
) -> dict[int, Velocity]:
    """Compute Velocity per product (keyed by product.product id).

    Direct velocity: gradual blend of Sage + Odoo.
    Component roll-up: for each BOM that has parent template T with direct
    monthly velocity V, every line that references variant C as a component
    contributes V * line.qty to component C's monthly demand.
    """
    n_weeks = weeks_since_cutover(today, cutover)
    logger.info("Cutover=%s, today=%s, N=%d weeks of Odoo data", cutover, today, n_weeks)

    # Build product lookups
    products_by_id = {p.id: p for p in products}
    template_to_products: dict[int, list[Product]] = {}
    for p in products:
        template_to_products.setdefault(p.template_id, []).append(p)

    # Aggregate Odoo sales by product variant id
    odoo_units_by_pid: dict[int, float] = {}
    for line in odoo_sale_lines:
        odoo_units_by_pid[line.product_id] = (
            odoo_units_by_pid.get(line.product_id, 0.0) + line.qty
        )

    # First pass: compute DIRECT velocity per product
    velocities: dict[int, Velocity] = {p.id: Velocity() for p in products}
    for p in products:
        sage_weekly = sage_weekly_by_sku.get(p.default_code, 0.0)
        # Odoo weekly = total odoo units / N weeks  (or 0 if no weeks yet)
        odoo_total = odoo_units_by_pid.get(p.id, 0.0)
        odoo_weekly = (odoo_total / n_weeks) if n_weeks > 0 else 0.0

        blended_weekly = _blend(sage_weekly, odoo_weekly, n_weeks)
        monthly_direct = blended_weekly * (WEEKS_PER_YEAR / MONTHS_PER_YEAR)

        v = velocities[p.id]
        v.sage_weekly = sage_weekly
        v.odoo_weekly = odoo_weekly
        v.blended_weekly = blended_weekly
        v.monthly_direct = monthly_direct

    # Second pass: BOM roll-up.
    # For each BOM (which produces a template), we need the *direct* monthly
    # velocity of that template. A template may have multiple variants
    # (e.g. variant by attribute), so we sum across them.
    #
    # We deliberately use *direct* parent velocity (not total) to avoid
    # double-counting if a component is itself the parent of another BOM.
    # Sun Co's typical use case is a single layer of BOMs (assembly of
    # purchased parts), so this is sufficient.
    direct_monthly_by_template: dict[int, float] = {}
    for p in products:
        direct_monthly_by_template[p.template_id] = (
            direct_monthly_by_template.get(p.template_id, 0.0)
            + velocities[p.id].monthly_direct
        )

    # Also include parent demand from any non-buy-route templates. Many
    # parents are manufactured (route=Manufacture) and won't be in
    # `products`, so we need their direct velocity too — fetched from
    # outside this function. For now, parent demand only flows from
    # buy-route parents. (We expand this if needed; see note below.)
    #
    # NOTE: If parent assemblies are NOT buy-route, this function won't
    # know their velocity and component roll-up will undercount. We
    # handle that by also receiving non-buy-route parent velocities
    # via the `parent_velocities` extension point — see run script.
    #
    # For the v1 build we keep it simple and only roll up from buy-route
    # parents that happen to have BOMs. Most Sun Co BOMs are on
    # manufactured assemblies, so the run script will compute parent
    # velocities for ALL templates that have a BOM, by passing those
    # template velocities back in. See `compute_velocities_with_parents`.

    # For the basic version, roll up from whatever direct velocities we know
    for bom in boms:
        if bom.bom_type == "phantom":
            # Kits: when the kit sells, components are consumed 1:1.
            # Same logic — parent monthly * line qty.
            pass
        parent_monthly = direct_monthly_by_template.get(bom.parent_template_id, 0.0)
        if parent_monthly <= 0 or bom.parent_qty <= 0:
            continue
        # parent_qty is qty produced per BOM run (usually 1). Scale line qty.
        for line in bom.lines:
            if line.component_product_id not in velocities:
                continue
            contribution = (
                parent_monthly * (line.qty_per_parent / bom.parent_qty)
            )
            velocities[line.component_product_id].monthly_component_rollup += contribution

    # Finalize totals
    for v in velocities.values():
        v.monthly_total = v.monthly_direct + v.monthly_component_rollup

    return velocities


def compute_velocities_with_parents(
    products: list[Product],
    sage_weekly_by_sku: dict[str, float],
    odoo_sale_lines: Iterable[SaleLine],
    boms: list[Bom],
    parent_skus_by_template: dict[int, str],
    today: date,
    cutover: date,
) -> dict[int, Velocity]:
    """Extended version that also considers parent (non-buy-route) templates.

    `parent_skus_by_template` maps template id -> SKU for ALL templates that
    have a BOM, including manufactured ones not on the Buy route. We use
    this to look up Sage/Odoo sales for the parents and roll their demand
    into their components.

    This is the function the main script uses.
    """
    n_weeks = weeks_since_cutover(today, cutover)
    logger.info("Cutover=%s, today=%s, N=%d weeks of Odoo data", cutover, today, n_weeks)

    # Build product lookups
    products_by_id = {p.id: p for p in products}
    buy_route_pids = set(products_by_id.keys())

    # Aggregate Odoo sales by product variant id (covers buy and non-buy)
    odoo_units_by_pid: dict[int, float] = {}
    for line in odoo_sale_lines:
        odoo_units_by_pid[line.product_id] = (
            odoo_units_by_pid.get(line.product_id, 0.0) + line.qty
        )

    # Compute direct monthly velocity for every product on Buy route
    velocities: dict[int, Velocity] = {p.id: Velocity() for p in products}
    for p in products:
        sage_weekly = sage_weekly_by_sku.get(p.default_code, 0.0)
        odoo_total = odoo_units_by_pid.get(p.id, 0.0)
        odoo_weekly = (odoo_total / n_weeks) if n_weeks > 0 else 0.0
        blended = _blend(sage_weekly, odoo_weekly, n_weeks)
        monthly = blended * (WEEKS_PER_YEAR / MONTHS_PER_YEAR)
        v = velocities[p.id]
        v.sage_weekly = sage_weekly
        v.odoo_weekly = odoo_weekly
        v.blended_weekly = blended
        v.monthly_direct = monthly

    # For parent templates that are NOT on Buy route, compute their direct
    # monthly velocity from Sage + Odoo and stash by template id.
    direct_monthly_by_template: dict[int, float] = {}
    # First: sum buy-route direct monthly per template
    for p in products:
        direct_monthly_by_template[p.template_id] = (
            direct_monthly_by_template.get(p.template_id, 0.0)
            + velocities[p.id].monthly_direct
        )

    # Build a fast lookup: which product_ids belong to each template
    # (needed to attribute Odoo sales to non-buy-route templates).
    # We don't have non-buy-route products in `products`, so we look at
    # sales lines and find ones whose product is not in buy_route_pids
    # — those belong to non-buy-route templates, but we don't know which
    # template without another fetch. Acceptable: the parent_skus_by_template
    # gives us SKU per template, and Sage velocity is keyed by SKU.
    # For Odoo non-buy-route parent velocity, we'd need a separate pull.
    #
    # For v1: we use Sage history alone for non-buy-route parent velocities,
    # which is fine because:
    #   1. The cutover is only ~3 weeks ago, so Odoo data is tiny.
    #   2. Sun Co's manufactured assemblies have stable demand patterns.
    #   3. As Odoo accumulates data, we can revisit and pull parent variant
    #      sales explicitly.
    for tmpl_id, sku in parent_skus_by_template.items():
        if tmpl_id in direct_monthly_by_template:
            continue  # already covered by buy-route products
        sage_weekly = sage_weekly_by_sku.get(sku, 0.0)
        # No Odoo data factored in for non-buy-route parents in v1.
        # The blend at the start of the year is essentially 100% Sage anyway.
        monthly = sage_weekly * (WEEKS_PER_YEAR / MONTHS_PER_YEAR)
        direct_monthly_by_template[tmpl_id] = monthly

    # Roll up BOM-driven component demand
    for bom in boms:
        parent_monthly = direct_monthly_by_template.get(bom.parent_template_id, 0.0)
        if parent_monthly <= 0 or bom.parent_qty <= 0:
            continue
        for line in bom.lines:
            if line.component_product_id not in velocities:
                continue  # component is not a buy-route product; skip
            contribution = parent_monthly * (line.qty_per_parent / bom.parent_qty)
            velocities[line.component_product_id].monthly_component_rollup += contribution

    # Finalize totals
    for v in velocities.values():
        v.monthly_total = v.monthly_direct + v.monthly_component_rollup

    return velocities
