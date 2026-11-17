"""Deterministic SQL gate.

The model proposes a single BigQuery SELECT. This module parses it with sqlglot and
either rejects it or rewrites every base-table reference into a PII-free, brand-scoped
CTE. Brand values are never interpolated: the rewritten text only contains the
query parameter `@entitled_brands`.
"""

from __future__ import annotations

from dataclasses import dataclass

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

from agent.safety.safe_schema import (
    ALLOWED_CATALOGS,
    ALLOWED_COLUMNS,
    ALLOWED_DATASETS,
    ALL_ALLOWED_COLUMNS,
    BASE_TABLES,
    DATASET,
    FORBIDDEN_COLUMNS,
    SAFE_TABLE,
)

_READ_ONLY = (exp.Select, exp.Union, exp.Intersect, exp.Except)

_SAFE_CTE_SQL = f"""
WITH safe_products AS (
  SELECT id, name, brand, category, department, cost, retail_price, sku, distribution_center_id
  FROM `{DATASET}.products`
  WHERE brand IN UNNEST(@entitled_brands)
),
safe_order_items AS (
  SELECT
    oi.id,
    oi.order_id,
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(oi.user_id AS STRING))) AS user_id,
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(oi.user_id AS STRING))) AS customer_key,
    oi.product_id,
    oi.inventory_item_id,
    oi.status,
    oi.sale_price,
    oi.created_at,
    oi.shipped_at,
    oi.delivered_at,
    oi.returned_at
  FROM `{DATASET}.order_items` AS oi
  JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
  WHERE p.brand IN UNNEST(@entitled_brands)
),
safe_orders AS (
  SELECT
    o.order_id,
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(o.user_id AS STRING))) AS user_id,
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(o.user_id AS STRING))) AS customer_key,
    o.status,
    o.gender,
    o.created_at,
    o.shipped_at,
    o.delivered_at,
    o.returned_at,
    o.num_of_item
  FROM `{DATASET}.orders` AS o
  WHERE o.order_id IN (
    SELECT oi.order_id
    FROM `{DATASET}.order_items` AS oi
    JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
    WHERE p.brand IN UNNEST(@entitled_brands)
  )
),
safe_users AS (
  SELECT
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(u.id AS STRING))) AS id,
    FARM_FINGERPRINT(CONCAT(@pii_salt, CAST(u.id AS STRING))) AS customer_key,
    u.age,
    u.gender,
    u.city,
    u.state,
    u.country,
    u.traffic_source,
    u.created_at
  FROM `{DATASET}.users` AS u
  WHERE u.id IN (
    SELECT oi.user_id
    FROM `{DATASET}.order_items` AS oi
    JOIN `{DATASET}.products` AS p ON oi.product_id = p.id
    WHERE p.brand IN UNNEST(@entitled_brands)
  )
)
SELECT 1
"""


@dataclass
class ValidationResult:
    ok: bool
    sql: str | None = None
    error: str | None = None


def validate_and_rewrite(sql: str, *, max_rows: int = 1000) -> ValidationResult:
    """Return rewritten SQL, or a reason the statement cannot run."""
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return ValidationResult(ok=False, error="The model returned an empty SQL statement.")
    if len(text) > 20_000:
        return ValidationResult(ok=False, error="SQL is too long.")

    try:
        statements = [s for s in sqlglot.parse(text, dialect="bigquery") if s is not None]
    except SqlglotError as exc:
        message = _ANSI.sub("", str(exc)).replace("\n", " ")
        return ValidationResult(ok=False, error=f"SQL syntax error: {message[:400]}")

    if len(statements) != 1:
        return ValidationResult(
            ok=False,
            error="Exactly one SELECT statement is allowed. Multiple statements were rejected.",
        )

    expr = statements[0]
    if isinstance(expr, exp.Subquery):
        expr = expr.this
    if not isinstance(expr, _READ_ONLY):
        return ValidationResult(
            ok=False,
            error=f"Only a single SELECT is allowed (got {type(expr).__name__}).",
        )

    blocked = _forbidden_node(expr)
    if blocked:
        return ValidationResult(ok=False, error=blocked)

    try:
        _reject_bad_tables(expr)
        _reject_cte_collisions(expr)
        _reject_forbidden_columns(expr)
        _reject_user_stars(expr)
    except ValueError as exc:
        return ValidationResult(ok=False, error=str(exc))

    _rename_base_tables(expr)
    expr = _cap_limit(expr, max_rows)
    expr = _prepend_safe_ctes(expr)
    rewritten = expr.sql(dialect="bigquery")
    if "@entitled_brands" not in rewritten or "@pii_salt" not in rewritten:
        return ValidationResult(ok=False, error="Scope rewrite failed to attach query parameters.")
    return ValidationResult(ok=True, sql=rewritten)


def _forbidden_node(expr: exp.Expression) -> str | None:
    names = (
        "Delete",
        "Drop",
        "Insert",
        "Update",
        "Create",
        "Merge",
        "Alter",
        "Command",
        "Export",
        "Grant",
        "Transaction",
        "Into",
    )
    types = tuple(getattr(exp, name) for name in names if hasattr(exp, name))
    found = next(expr.find_all(*types), None) if types else None
    if found is not None:
        return f"Statement type {type(found).__name__} is not allowed. Analysis is read-only."
    return None


def _cte_names(expr: exp.Expression) -> set[str]:
    names = set()
    for cte in expr.find_all(exp.CTE):
        alias = cte.alias
        if not isinstance(alias, str):
            alias = getattr(cte, "alias_or_name", "") or ""
        if alias:
            names.add(alias.lower())
    return names


def _reject_cte_collisions(expr: exp.Expression) -> None:
    reserved = set(BASE_TABLES) | set(SAFE_TABLE.values())
    collision = _cte_names(expr) & reserved
    if collision:
        raise ValueError(
            "CTE name collides with an analytical table: " + ", ".join(sorted(collision))
        )


def _reject_bad_tables(expr: exp.Expression) -> None:
    cte_names = _cte_names(expr)
    for table in expr.find_all(exp.Table):
        name = table.name.lower()
        if name in cte_names:
            continue
        catalog = (table.catalog or "").lower()
        db = (table.db or "").lower()
        if catalog not in ALLOWED_CATALOGS or db not in ALLOWED_DATASETS:
            raise ValueError(f"Cross-dataset reference `{table.sql(dialect='bigquery')}` is not allowed.")
        if name not in BASE_TABLES:
            raise ValueError(f"Table `{name}` is not in the analytical schema.")


def _reject_forbidden_columns(expr: exp.Expression) -> None:
    aliases = {a.alias.lower() for a in expr.find_all(exp.Alias) if a.alias}
    for column in expr.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        name = column.name.lower()
        if name in FORBIDDEN_COLUMNS:
            raise ValueError(
                f"Column `{name}` is personal data and is not in the analytical schema. "
                "Use customer_key, or aggregate by state, age, gender, or traffic_source."
            )
        if name in aliases or name in ALL_ALLOWED_COLUMNS:
            continue
        raise ValueError(
            f"Column `{name}` is not in the analytical schema. "
            "Use only business columns and the hashed customer_key."
        )


def _reject_user_stars(expr: exp.Expression) -> None:
    for select in expr.find_all(exp.Select):
        direct = _direct_tables(select)
        user_aliases = set()
        reads_users = False
        for table in direct:
            if table.name.lower() == "users":
                reads_users = True
                if table.alias:
                    user_aliases.add(str(table.alias).lower())
                user_aliases.add("users")
        if not reads_users:
            continue
        for proj in select.expressions:
            if not _is_star(proj):
                continue
            node = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(node, exp.Star):
                raise ValueError("SELECT * FROM users is not allowed. List the analytical columns you need.")
            if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
                qualifier = (node.table or "").lower()
                if not qualifier or qualifier in user_aliases:
                    raise ValueError("SELECT * FROM users is not allowed. List the analytical columns you need.")


def _direct_tables(select: exp.Select) -> list[exp.Table]:
    tables: list[exp.Table] = []
    from_ = select.args.get("from_")
    if from_ is not None and isinstance(from_.this, exp.Table):
        tables.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def _is_star(proj: exp.Expression) -> bool:
    node = proj.this if isinstance(proj, exp.Alias) else proj
    if isinstance(node, exp.Star):
        return True
    return isinstance(node, exp.Column) and isinstance(node.this, exp.Star)


def _rename_base_tables(expr: exp.Expression) -> None:
    cte_names = _cte_names(expr)
    for table in list(expr.find_all(exp.Table)):
        name = table.name.lower()
        if name in cte_names or name not in SAFE_TABLE:
            continue
        table.set("this", exp.to_identifier(SAFE_TABLE[name], quoted=False))
        table.set("catalog", None)
        table.set("db", None)


def _cap_limit(expr: exp.Expression, cap: int) -> exp.Expression:
    if not isinstance(expr, exp.Select):
        wrapped = exp.Select(expressions=[exp.Star()]).from_(
            exp.From(this=exp.Subquery(this=expr.copy(), alias=exp.to_identifier("capped")))
        )
        return wrapped.limit(cap)
    current = expr.args.get("limit")
    if current is None:
        return expr.limit(cap)
    try:
        literal = current.expression
        number = int(literal.this if isinstance(literal, exp.Literal) else str(literal))
    except (TypeError, ValueError, AttributeError):
        return expr.limit(cap)
    if number > cap:
        return expr.limit(cap)
    return expr


def _prepend_safe_ctes(expr: exp.Expression) -> exp.Expression:
    seed = sqlglot.parse_one(_SAFE_CTE_SQL, dialect="bigquery")
    safe = [cte.copy() for cte in seed.ctes]
    existing = [cte.copy() for cte in expr.ctes] if expr.args.get("with_") else []
    expr.set("with_", exp.With(expressions=[*safe, *existing]))
    return expr
