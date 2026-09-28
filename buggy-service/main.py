"""
Order Service — Fast API service with optimized batch processing.

The POST /orders endpoint uses batched inventory lookups to maintain
sub-linear performance scaling with order size, keeping p99 latency
well under the 500ms SLO target.
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
# Fixed: Batched DB queries
# ---------------------------------------------------------------------------
def _simulate_batched_db_lookup(skus: list[str]) -> dict[str, dict]:
    """Simulates a batched DB lookup for multiple SKUs in a single query."""
    # Simulate a single batched DB call - latency doesn't scale with item count
    delay = random.uniform(0.08, 0.15)  # Single query time regardless of SKU count
    time.sleep(delay)
    
    # Return results for all SKUs in one operation
    results = {}
    for sku in skus:
        results[sku] = {
            "sku": sku, 
            "price": round(random.uniform(9.99, 99.99), 2), 
            "in_stock": True
        }
    return results


def _simulate_payment_validation(customer_id: str) -> bool:
    """Simulates a payment gateway call."""
    # Payment validation time doesn't depend on order size
    time.sleep(random.uniform(0.02, 0.08))
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
    Create an order with batched inventory lookups for optimal performance.
    - All SKU lookups are batched into a single DB query
    - Payment validation is independent of order size
    - Performance now scales sub-linearly with item count, staying well under 500ms SLO
    """
    start = time.monotonic()

    # Batched lookup: single query for ALL SKUs in the order
    skus = [item.sku for item in order.items]
    inventory_results = _simulate_batched_db_lookup(skus)

    # Single payment validation call
    _simulate_payment_validation(order.customer_id)

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
