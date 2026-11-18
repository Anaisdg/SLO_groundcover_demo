"""
Order Service.

POST /orders previously had a simulated N+1 query pattern (two sequential
"DB lookups" per line item plus a sequential payment call), which pushed p99
latency well past the 500ms SLO. This version does ONE batched lookup for all
SKUs and runs payment validation concurrently with it.
"""

import time
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="order-service", version="0.2.0")

_executor = ThreadPoolExecutor(max_workers=8)


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
# Fix: one batched "DB query" for all SKUs (replaces N+1 per-item lookups)
# ---------------------------------------------------------------------------
def _simulate_batch_db_lookup(skus: list[str]) -> list[dict]:
    """Simulates a single DB round-trip (e.g. WHERE sku IN (...)) for all items."""
    time.sleep(random.uniform(0.04, 0.10))
    return [
        {"sku": sku, "price": round(random.uniform(9.99, 99.99), 2), "in_stock": True}
        for sku in skus
    ]


def _simulate_payment_validation(customer_id: str) -> bool:
    """Simulates a payment gateway call."""
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
    """Create an order: one batched lookup, payment validated concurrently."""
    start = time.monotonic()

    skus = [item.sku for item in order.items]
    lookup_future = _executor.submit(_simulate_batch_db_lookup, skus)
    payment_future = _executor.submit(_simulate_payment_validation, order.customer_id)
    lookup_future.result()
    payment_future.result()

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
