"""Input validation for the one console action that takes user input (Step 8 write).

The console URL is public (deploy.sh grants allUsers), so every value that ends
up inside an agent prompt is pattern-checked and must exist in BigQuery first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.cloud import bigquery

import clients as c

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AMOUNT_MIN = 1.0
AMOUNT_MAX = 10_000.0


@dataclass(frozen=True)
class WriteRequest:
    """A validated Step 8 write request.

    Attributes:
        engine: "Spanner" or "AlloyDB".
        customer_id: An existing customer id.
        product_id: An existing product id.
        amount: Order amount within bounds.
    """

    engine: str
    customer_id: str
    product_id: str
    amount: float


def allowed_engines() -> list[str]:
    """Returns the engines an agent may be asked to write to in this event."""
    return ["Spanner"] + (["AlloyDB"] if c.ALLOYDB_ENABLED else [])


def parse_write(engine: str, customer_id: str, product_id: str, amount: str) -> WriteRequest:
    """Validates raw form values and returns a WriteRequest.

    Args:
        engine: Target engine name from the form.
        customer_id: Customer id from the form.
        product_id: Product id from the form.
        amount: Amount from the form, as text.

    Returns:
        The validated request.

    Raises:
        ValueError: If any value fails its pattern, bound or existence check.
    """
    if engine not in allowed_engines():
        raise ValueError(f"engine must be one of {allowed_engines()}")
    customer_id, product_id = customer_id.strip(), product_id.strip()
    for name, value in (("customer_id", customer_id), ("product_id", product_id)):
        if not ID_PATTERN.fullmatch(value):
            raise ValueError(f"{name} must match {ID_PATTERN.pattern}")
    try:
        amount_f = float(amount)
    except ValueError as exc:
        raise ValueError("amount must be a number") from exc
    if not AMOUNT_MIN <= amount_f <= AMOUNT_MAX:
        raise ValueError(f"amount must be between {AMOUNT_MIN:.0f} and {AMOUNT_MAX:,.0f}")

    found = c.run_query(
        f"""
        SELECT
          EXISTS(SELECT 1 FROM `{c.PROJECT_ID}.{c.SOURCE_DATASET}.customers` WHERE customer_id = @cid) AS has_c,
          EXISTS(SELECT 1 FROM `{c.PROJECT_ID}.{c.SOURCE_DATASET}.products`  WHERE product_id  = @pid) AS has_p
        """,
        params=[
            bigquery.ScalarQueryParameter("cid", "STRING", customer_id),
            bigquery.ScalarQueryParameter("pid", "STRING", product_id),
        ],
    )
    if not found.first("has_c"):
        raise ValueError(f"customer_id {customer_id} does not exist")
    if not found.first("has_p"):
        raise ValueError(f"product_id {product_id} does not exist")
    return WriteRequest(engine, customer_id, product_id, amount_f)
