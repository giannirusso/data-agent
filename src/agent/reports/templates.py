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


def template_summary(period: ReportPeriod, frames: list[dict]) -> str:
    """Used when the single narrative call times out or is skipped."""
    lines = [
        f"TITLE: {period.label} performance and next-quarter actions",
        period.note,
        "",
        "## Summary",
        "Template-based summary. The narrative model did not answer, so these bullets are the query results themselves.",
    ]
    for frame in frames:
        title = frame.get("question") or "Result"
        rows = frame.get("rows") or []
        if frame.get("empty") or not rows:
            lines.append(f"- {title}: no rows in this period.")
            continue
        preview = "; ".join(_row_text(row) for row in rows[:5])
        lines.append(f"- {title}: {preview}")
    lines.extend(
        [
            "",
            "## Insights",
            "Compare the monthly revenue line with the top category and the return rate before treating one product as the whole story.",
            "",
            "## Action items (template-based, not model-written)",
            "1) Review the top category and the top product with the commercial team before the next quarter.",
            "2) If the return rate is high versus the previous quarter, inspect that category before reordering.",
            "3) Put the next quarter's plan around the products already carrying revenue, and check new versus returning customers.",
        ]
    )
    return "\n".join(lines)


def _row_text(row: dict) -> str:
    parts = []
    for key, value in row.items():
        if isinstance(value, float):
            value = round(value, 2)
        parts.append(f"{key}={value}")
    return ", ".join(parts)
