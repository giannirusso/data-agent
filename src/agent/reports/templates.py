"""Parameterized SQL for a quarterly report.

Each statement still goes through the sqlglot gate, brand-scope rewrite,
dry run, and byte cap. Dates are query parameters, not pasted into the text.
"""

from __future__ import annotations

from agent.reports.period import ReportPeriod

_WINDOW = """
  AND created_at >= @period_start
  AND created_at < @period_end
""".strip()

_ITEM_WINDOW = """
  AND oi.created_at >= @period_start
  AND oi.created_at < @period_end
""".strip()

_OPEN = "status NOT IN ('Cancelled', 'Returned')"
_ITEM_OPEN = "oi.status NOT IN ('Cancelled', 'Returned')"


def report_steps(period: ReportPeriod) -> list[dict]:
    params = period.params
    steps = [
        (
            "monthly",
            f"Revenue, orders, and average order value by month in {period.label}",
            f"""
            SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month,
                   ROUND(SUM(sale_price), 2) AS revenue,
                   COUNT(DISTINCT order_id) AS orders,
                   ROUND(SUM(sale_price) / NULLIF(COUNT(DISTINCT order_id), 0), 2) AS aov
            FROM order_items
            WHERE {_OPEN}
              {_WINDOW}
            GROUP BY month
            ORDER BY month
            """,
        ),
        (
            "products",
            f"Top products by revenue in {period.label}",
            f"""
            SELECT p.name, ROUND(SUM(oi.sale_price), 2) AS revenue,
                   COUNT(DISTINCT oi.order_id) AS orders
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE {_ITEM_OPEN}
              {_ITEM_WINDOW}
            GROUP BY p.name
            ORDER BY revenue DESC
            LIMIT 5
            """,
        ),
        (
            "categories",
            f"Top categories by revenue in {period.label}",
            f"""
            SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS revenue,
                   COUNT(*) AS items
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            WHERE {_ITEM_OPEN}
              {_ITEM_WINDOW}
            GROUP BY p.category
            ORDER BY revenue DESC
            LIMIT 5
            """,
        ),
        (
            "returns",
            f"Return rate in {period.label}",
            f"""
            SELECT COUNTIF(status = 'Returned') AS returned_items,
                   COUNT(*) AS items,
                   ROUND(COUNTIF(status = 'Returned') / NULLIF(COUNT(*), 0), 4) AS return_rate
            FROM order_items
            WHERE created_at >= @period_start AND created_at < @period_end
            """,
        ),
        (
            "customers",
            f"New versus returning customers in {period.label}",
            f"""
            WITH first_order AS (
              SELECT user_id, MIN(created_at) AS first_at
              FROM order_items
              WHERE {_OPEN}
              GROUP BY user_id
            ),
            period_buyers AS (
              SELECT DISTINCT user_id
              FROM order_items
              WHERE {_OPEN}
                {_WINDOW}
            )
            SELECT COUNTIF(first_at >= @period_start AND first_at < @period_end) AS new_customers,
                   COUNTIF(first_at < @period_start) AS returning_customers
            FROM period_buyers
            JOIN first_order ON period_buyers.user_id = first_order.user_id
            """,
        ),
        (
            "qoq",
            f"{period.label} compared with the previous quarter",
            f"""
            SELECT CASE
                     WHEN created_at >= @period_start AND created_at < @period_end THEN 'current'
                     ELSE 'previous'
                   END AS period,
                   ROUND(SUM(sale_price), 2) AS revenue,
                   COUNT(DISTINCT order_id) AS orders
            FROM order_items
            WHERE {_OPEN}
              AND created_at >= @previous_start
              AND created_at < @period_end
            GROUP BY period
            ORDER BY period
            """,
        ),
    ]
    return [
        {
            "question": question,
            "purpose": purpose,
            "sql": " ".join(sql.split()),
            "params": params,
            "template": True,
        }
        for purpose, question, sql in steps
    ]


def template_summary(period: ReportPeriod, frames: list[dict], *, skip_hint: str | None = None) -> str:
    """Executive bullets used when the narrative call does not return text."""
    facts = _facts(frames)
    lines = [f"TITLE: {period.label} performance and next-quarter actions", ""]
    if skip_hint:
        lines.extend([skip_hint, ""])
    lines.extend([period.note, "", "## Summary"])
    for bullet in _summary_bullets(facts):
        lines.append(f"- {bullet}")
    lines.extend(["", "## Insights", _insight(facts), "", "## Action items (template-based, not model-written)"])
    for index, item in enumerate(_actions(facts), start=1):
        lines.append(f"{index}) {item}")
    return "\n".join(lines)


def _facts(frames: list[dict]) -> dict:
    by_purpose = {}
    for frame in frames:
        purpose = frame.get("purpose")
        if purpose:
            by_purpose[purpose] = frame.get("rows") or []
    current, previous = _period_pair(by_purpose.get("qoq") or [])
    if current.get("revenue") is None:
        current = _sum_rows(by_purpose.get("monthly") or []) or current
    revenue = current.get("revenue")
    orders = current.get("orders")
    aov = current.get("aov")
    if aov is None and revenue is not None and orders:
        aov = revenue / orders
    prev_revenue = previous.get("revenue")
    prev_orders = previous.get("orders")
    prev_aov = previous.get("aov")
    if prev_aov is None and prev_revenue is not None and prev_orders:
        prev_aov = prev_revenue / prev_orders
    category = _first(by_purpose.get("categories") or [])
    product = _first(by_purpose.get("products") or [])
    returns = _first(by_purpose.get("returns") or [])
    customers = _first(by_purpose.get("customers") or [])
    returned = _num(returns, "returned_items")
    items = _num(returns, "items")
    rate = _num(returns, "return_rate")
    if rate is None and returned is not None and items:
        rate = returned / items
    if rate is not None and rate > 1:
        rate = rate / 100.0
    new_customers = _num(customers, "new_customers")
    returning = _num(customers, "returning_customers")
    return {
        "revenue": revenue,
        "orders": orders,
        "aov": aov,
        "revenue_change": _change(revenue, prev_revenue),
        "orders_change": _change(orders, prev_orders),
        "aov_change": _change(aov, prev_aov),
        "category": _text(category, "category", "name"),
        "category_revenue": _num(category, "revenue"),
        "product": _text(product, "name"),
        "product_revenue": _num(product, "revenue"),
        "return_rate": rate,
        "returned_items": returned,
        "return_items": items,
        "new_customers": new_customers,
        "returning_customers": returning,
    }


def _summary_bullets(facts: dict) -> list[str]:
    bullets = []
    if facts["revenue"] is not None or facts["orders"] is not None:
        bits = []
        if facts["revenue"] is not None:
            bits.append(f"Revenue: {_money(facts['revenue'])}{_delta(facts['revenue_change'])}")
        if facts["orders"] is not None:
            orders = _count(facts["orders"])
            bits.append(f"{orders} orders{_delta(facts['orders_change'])}")
        if facts["aov"] is not None:
            bits.append(f"AOV {_money(facts['aov'])}{_delta(facts['aov_change'])}")
        bullets.append(", ".join(bits))
    if facts["category"]:
        extra = f", {_money(facts['category_revenue'])}" if facts["category_revenue"] is not None else ""
        bullets.append(f"Top category: {facts['category']}{extra}")
    if facts["product"]:
        extra = f", {_money(facts['product_revenue'])}" if facts["product_revenue"] is not None else ""
        bullets.append(f"Top product: {facts['product']}{extra}")
    if facts["return_rate"] is not None:
        detail = ""
        if facts["returned_items"] is not None and facts["return_items"] is not None:
            detail = f" ({_count(facts['returned_items'])} of {_count(facts['return_items'])} items)"
        bullets.append(f"Return rate {_percent(facts['return_rate'])}{detail}")
    if facts["new_customers"] is not None or facts["returning_customers"] is not None:
        new = _count(facts["new_customers"] or 0)
        returning = _count(facts["returning_customers"] or 0)
        bullets.append(f"Customers: {new} new, {returning} returning")
    if not bullets:
        bullets.append("No rows came back for this period.")
    return bullets


def _insight(facts: dict) -> str:
    parts = []
    if facts["revenue_change"] is not None:
        direction = "up" if facts["revenue_change"] >= 0 else "down"
        parts.append(f"Revenue is {direction} {_percent(abs(facts['revenue_change']))} versus the previous quarter.")
    if facts["category"]:
        parts.append(f"{facts['category']} is the largest category in this view.")
    if facts["return_rate"] is not None and facts["return_rate"] > 0.08:
        parts.append("Returns are high enough to look at before the next buy.")
    if not parts:
        return "The figures above are the template aggregates for this quarter."
    return " ".join(parts)


def _actions(facts: dict) -> list[str]:
    items = []
    if facts["return_rate"] is not None and facts["return_rate"] > 0.08:
        where = f" in {facts['category']}" if facts["category"] else ""
        items.append(f"Return rate is above 8%{where}. Check fit and quality before reordering.")
    if facts["revenue_change"] is not None and facts["revenue_change"] < 0:
        items.append("Revenue is down versus last quarter. Revisit the assortment that carried the previous quarter before locking the next buy.")
    new = facts["new_customers"]
    returning = facts["returning_customers"]
    if new is not None and returning is not None and (new + returning) > 0:
        share = returning / (new + returning)
        if share < 0.15:
            items.append("Returning customers are a small share of buyers. Add a retention action next quarter, such as a reorder reminder.")
    if not items:
        items.append("No template rule fired. Keep the current top category in stock and review this quarter's mix with the commercial team.")
    return items


def _period_pair(rows: list[dict]) -> tuple[dict, dict]:
    current: dict = {}
    previous: dict = {}
    unlabeled = []
    for row in rows:
        label = str(row.get("period") or "").lower()
        if label == "current":
            current = row
        elif label == "previous":
            previous = row
        else:
            unlabeled.append(row)
    if not current and len(unlabeled) == 1:
        current = unlabeled[0]
    return current, previous


def _sum_rows(rows: list[dict]) -> dict:
    if not rows:
        return {}
    total = {"revenue": 0.0, "orders": 0.0}
    seen = False
    for row in rows:
        revenue = _num(row, "revenue")
        orders = _num(row, "orders")
        if revenue is None and orders is None:
            continue
        seen = True
        total["revenue"] += revenue or 0
        total["orders"] += orders or 0
    if not seen:
        return {}
    if total["orders"]:
        total["aov"] = total["revenue"] / total["orders"]
    return total


def _first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def _num(row: dict, *keys: str) -> float | None:
    for key in keys:
        if key not in row or row[key] is None:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            continue
    return None


def _text(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return None


def _change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _count(value: float) -> str:
    return f"{int(round(value)):,}"


def _percent(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _delta(change: float | None) -> str:
    if change is None:
        return ""
    sign = "+" if change > 0 else ""
    return f" ({sign}{change * 100:.1f}% vs previous quarter)"
