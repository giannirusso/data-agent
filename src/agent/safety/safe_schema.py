"""Allowed analytical schema. PII columns are absent on purpose."""

from __future__ import annotations

DATASET = "bigquery-public-data.thelook_ecommerce"

BASE_TABLES = ("products", "order_items", "orders", "users")

SAFE_TABLE = {
    "products": "safe_products",
    "order_items": "safe_order_items",
    "orders": "safe_orders",
    "users": "safe_users",
}

# Direct identifiers and the postal_code quasi-identifier. Never selectable.
FORBIDDEN_COLUMNS = frozenset(
    {
        "first_name",
        "last_name",
        "email",
        "street_address",
        "latitude",
        "longitude",
        "postal_code",
    }
)

ALLOWED_COLUMNS: dict[str, frozenset[str]] = {
    "products": frozenset(
        {
            "id",
            "name",
            "brand",
            "category",
            "department",
            "cost",
            "retail_price",
            "sku",
            "distribution_center_id",
        }
    ),
    "order_items": frozenset(
        {
            "id",
            "order_id",
            "user_id",
            "customer_key",
            "product_id",
            "inventory_item_id",
            "status",
            "sale_price",
            "created_at",
            "shipped_at",
            "delivered_at",
            "returned_at",
        }
    ),
    "orders": frozenset(
        {
            "order_id",
            "user_id",
            "customer_key",
            "status",
            "gender",
            "created_at",
            "shipped_at",
            "delivered_at",
            "returned_at",
            "num_of_item",
        }
    ),
    "users": frozenset(
        {
            "id",
            "customer_key",
            "age",
            "gender",
            "city",
            "state",
            "country",
            "traffic_source",
            "created_at",
        }
    ),
}

ALL_ALLOWED_COLUMNS = frozenset().union(*ALLOWED_COLUMNS.values())

ALLOWED_CATALOGS = frozenset({"", "bigquery-public-data"})
ALLOWED_DATASETS = frozenset({"", "thelook_ecommerce"})


def sql_prompt_schema() -> str:
    """Short schema for the SQL prompt. The user-facing catalog is much longer."""
    return "\n".join(
        [
            "products(id, name, brand, category, department, cost, retail_price, sku, distribution_center_id)",
            "order_items(id, order_id, user_id, customer_key, product_id, status, sale_price, created_at, shipped_at, delivered_at, returned_at)",
            "orders(order_id, user_id, customer_key, status, gender, num_of_item, created_at, shipped_at, delivered_at, returned_at)",
            "users(id, customer_key, age, gender, city, state, country, traffic_source, created_at)",
            "No first_name, last_name, email, street_address, postal_code, latitude, longitude.",
            "Round money with ROUND(expr, 2). Revenue excludes status Cancelled and Returned.",
        ]
    )


def catalog_markdown() -> str:
    """Safe catalog shown for schema questions. No SQL is run."""
    return "\n".join(
        [
            "I can analyse the public Looker e-commerce dataset, limited to your brands.",
            "Personal details are not in the analytical schema.",
            "",
            "- **products** — id, name, brand, category, department, cost, retail_price, sku, distribution_center_id",
            "- **order_items** — id, order_id, customer_key (hashed user), product_id, status, sale_price, created/shipped/delivered/returned timestamps",
            "- **orders** — order_id, customer_key, status, gender, item count, timestamps",
            "- **users** (aggregates) — customer_key, age, gender, city, state, country, traffic_source, created_at",
            "",
            "Not available: names, email, street address, postal code, latitude, longitude.",
            "",
            "Revenue means the sum of `sale_price` on items that are not Cancelled or Returned.",
            "Margin means `sale_price` minus product cost, on those same items.",
            "A customer is identified only by a salted hash (`customer_key`), so repeat behaviour can be measured without exposing who they are.",
            "",
            "Ask about customer behaviour, product performance, or a time period. I can also save a report.",
        ]
    )
