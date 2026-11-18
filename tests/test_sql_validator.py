"""SQL gate: read-only, no PII columns, brand scope as a query parameter."""

from agent.cli import handle_message
from agent.safety.sql_validator import validate_and_rewrite
from tests.conftest import FakeBQ, make_runtime

VALID = (
    "SELECT u.state, SUM(oi.sale_price) AS revenue "
    "FROM order_items oi JOIN users u ON oi.user_id = u.id "
    "WHERE oi.status NOT IN ('Cancelled','Returned') GROUP BY u.state"
)


def test_delete_drop_and_multi_statement_rejected():
    delete = validate_and_rewrite("DELETE FROM orders WHERE 1=1")
    assert not delete.ok
    assert "SELECT" in delete.error

    drop = validate_and_rewrite("DROP TABLE orders")
    assert not drop.ok

    multi = validate_and_rewrite("SELECT sale_price FROM order_items; DROP TABLE orders")
    assert not multi.ok
    assert "one" in multi.error.lower()


def test_pii_columns_and_star_on_users_rejected():
    email = validate_and_rewrite("SELECT email FROM users")
    assert not email.ok
    assert "email" in email.error

    postal = validate_and_rewrite("SELECT postal_code, city FROM users")
    assert not postal.ok

    star = validate_and_rewrite("SELECT * FROM users")
    assert not star.ok
    assert "*" in star.error

    qualified_star = validate_and_rewrite("SELECT u.* FROM users u")
    assert not qualified_star.ok


def test_products_star_is_rewritten_with_scope_params_and_limit():
    result = validate_and_rewrite("SELECT * FROM products")
    assert result.ok
    assert "@entitled_brands" in result.sql
    assert "@pii_salt" in result.sql
    assert "FARM_FINGERPRINT" in result.sql
    assert "safe_products" in result.sql
    assert "LIMIT 1000" in result.sql
    assert "Calvin Klein" not in result.sql


def test_cross_project_and_unknown_column_rejected():
    cross = validate_and_rewrite("SELECT id FROM `other-project.thelook_ecommerce.products`")
    assert not cross.ok
    unknown = validate_and_rewrite("SELECT not_a_column FROM products")
    assert not unknown.ok


def test_qualified_public_table_is_rewritten_to_safe_cte():
    result = validate_and_rewrite(
        "SELECT sale_price FROM `bigquery-public-data.thelook_ecommerce.order_items`"
    )
    assert result.ok
    assert "safe_order_items" in result.sql
    assert "thelook_ecommerce.order_items`" in result.sql  # only inside the safe CTE


def test_over_budget_query_is_not_executed(settings):
    bq = FakeBQ(over_budget=True)
    calls = {"n": 0}

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            calls["n"] += 1
            return (
                "SELECT SUM(sale_price) AS revenue "
                f"FROM order_items WHERE status NOT IN ('Cancelled','Returned') "
                f"/* attempt {calls['n']} */"
            )
        return "I could not stay inside the byte budget."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    reply = handle_message(runtime, "Monthly revenue in 2023")
    assert bq.executed == []
    assert bq.dry_runs
    assert "budget" in reply.lower() or "bytes" in reply.lower()
    assert "trace:" in reply


def test_alice_scope_is_a_parameter_not_a_literal(settings):
    bq = FakeBQ()

    def complete(system, user, tier):
        if "PURPOSE: sql" in system:
            return VALID
        return "Texas revenue is in the table."

    runtime = make_runtime(settings, bq=bq, complete=complete)
    handle_message(runtime, "Monthly revenue in 2023")
    assert bq.executed
    sql = bq.executed[0]["sql"]
    params = bq.executed[0]["params"]
    assert params["entitled_brands"] == ["Calvin Klein", "Allegra K"]
    assert params["pii_salt"] == "test-salt"
    assert "@entitled_brands" in sql
    assert "Calvin Klein" not in sql
    assert "Allegra K" not in sql
    assert bq.executed[0]["maximum_bytes_billed"] == settings.bq_max_bytes

    bob = FakeBQ()
    runtime_bob = make_runtime(settings, user="bob", session_id="bob-sess", bq=bob, complete=complete)
    handle_message(runtime_bob, "Monthly revenue in 2023")
    assert bob.executed[0]["params"]["entitled_brands"] == ["Tommy Hilfiger"]
    assert "Tommy Hilfiger" not in bob.executed[0]["sql"]
