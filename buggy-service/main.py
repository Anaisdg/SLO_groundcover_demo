"""
Buggy Order Service — intentionally slow endpoint for SLO breach demo.

The POST /orders endpoint has a simulated N+1 query pattern:
for each item in an order, it does a sequential "database lookup" (time.sleep).
This causes p99 latency to blow past the 500ms SLO target.
"""

import time
import random
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="order-service", version="0.1.0")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class OrderItem(BaseModel):
    sku: str
    quantity: int = 1


class OrderRequest(BaseModel):
    customer_id: str
    items: list[OrderItem]


class OrderResponse(BaseModel):
    order_id: str
    status: str
    total_ms: float
    items_validated: int
    timestamp: str


# ---------------------------------------------------------------------------
# The Bug: N+1 sequential "DB queries" per item
# ---------------------------------------------------------------------------
def _simulate_db_lookup(sku: str, *, multi_line_order: bool) -> dict:
    """Simulates a DB round-trip per line item; multi-line orders stack cost (N+1 demo)."""
    if multi_line_order:
        delay = random.uniform(0.12, 0.22)
    else:
        # Single-line cart: keep total work clearly under typical 500ms SLO.
        delay = random.uniform(0.04, 0.10)
    time.sleep(delay)
    return {"sku": sku, "price": round(random.uniform(9.99, 99.99), 2), "in_stock": True}


def _simulate_payment_validation(customer_id: str, *, multi_line_order: bool) -> bool:
    """Simulates a payment gateway call."""
    if multi_line_order:
        time.sleep(random.uniform(0.05, 0.12))
    else:
        time.sleep(random.uniform(0.02, 0.06))
    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/readyz")
def ready():
    return {"status": "ready"}


@app.post("/orders", response_model=OrderResponse)
def create_order(order: OrderRequest):
    """
    Create an order. This is intentionally buggy:
    - Each item triggers a sequential DB lookup (N+1 pattern)
    - Payment validation adds another blocking call
    - With 2+ line items, sequential per-item work exceeds the 500ms SLO; single-line stays under
    """
    start = time.monotonic()
    multi_line_order = len(order.items) >= 2

    # N+1: sequential lookup for EACH line item (the bug shows up once count >= 2)
    for item in order.items:
        _simulate_db_lookup(item.sku, multi_line_order=multi_line_order)
        _simulate_db_lookup(item.sku, multi_line_order=multi_line_order)

    _simulate_payment_validation(order.customer_id, multi_line_order=multi_line_order)

    elapsed_ms = (time.monotonic() - start) * 1000

    return OrderResponse(
        order_id=str(uuid.uuid4()),
        status="created",
        total_ms=round(elapsed_ms, 1),
        items_validated=len(order.items),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/catalog")
@app.get("/products", include_in_schema=False)
def list_catalog():
    """Read-only SKU list — no DB simulation. `/products` kept as a legacy alias for the same handler."""
    return {
        "skus": [
            {"sku": "WIDGET-001", "name": "Widget", "price": 29.99},
            {"sku": "GADGET-002", "name": "Gadget", "price": 49.99},
            {"sku": "GIZMO-003", "name": "Gizmo", "price": 19.99},
        ]
    }
