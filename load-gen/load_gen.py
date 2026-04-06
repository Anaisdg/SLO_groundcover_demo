#!/usr/bin/env python3
"""
Load generator for the buggy order-service.

Sends a mix of requests:
- 70% POST /orders with 3–5 line items (slow — typical SLO breach)
- 20% POST /orders with 1 line item (kept under SLO by service tuning)
- 10% GET /catalog (fast — baseline)

Usage:
    python load_gen.py                          # default: http://localhost:8000
    python load_gen.py http://order-service.slo-demo.svc.cluster.local
    python load_gen.py http://localhost:8000 --rps 5 --duration 300
"""

import argparse
import random
import time
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
from urllib.error import URLError

SKUS = ["WIDGET-001", "GADGET-002", "GIZMO-003", "DOOHICKEY-004", "THINGAMAJIG-005"]


def send_order(base_url: str, num_line_items: int) -> dict:
    """Send POST /orders with the given number of order line items (SKUs in the cart)."""
    line_items = [{"sku": random.choice(SKUS), "quantity": random.randint(1, 3)} for _ in range(num_line_items)]
    payload = json.dumps({"customer_id": f"cust-{random.randint(1000, 9999)}", "items": line_items}).encode()

    req = Request(
        f"{base_url}/orders",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.monotonic()
    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            elapsed = (time.monotonic() - start) * 1000
            return {
                "status": resp.status,
                "elapsed_ms": round(elapsed, 1),
                "server_ms": body.get("total_ms"),
                "kind": "order",
                "line_items": num_line_items,
            }
    except URLError as e:
        elapsed = (time.monotonic() - start) * 1000
        return {
            "status": "error",
            "elapsed_ms": round(elapsed, 1),
            "error": str(e),
            "kind": "order",
            "line_items": num_line_items,
        }


def send_catalog(base_url: str) -> dict:
    """GET /catalog — static list, no order line items involved."""
    req = Request(f"{base_url}/catalog", method="GET")
    start = time.monotonic()
    try:
        with urlopen(req, timeout=10) as resp:
            elapsed = (time.monotonic() - start) * 1000
            return {"status": resp.status, "elapsed_ms": round(elapsed, 1), "kind": "catalog"}
    except URLError as e:
        elapsed = (time.monotonic() - start) * 1000
        return {"status": "error", "elapsed_ms": round(elapsed, 1), "error": str(e), "kind": "catalog"}


def _format_result_row(result: dict) -> str:
    if result.get("kind") == "catalog":
        return "GET /catalog"
    n = result.get("line_items", 0)
    return f"POST /orders ({n} line item{'s' if n != 1 else ''})"


def run_load(base_url: str, rps: float, duration: int):
    """Run load generation at the specified rate."""
    interval = 1.0 / rps
    end_time = time.monotonic() + duration
    total = 0
    slow_count = 0

    print(f"Generating load against {base_url}")
    print(f"  Target RPS: {rps}  |  Duration: {duration}s")
    print(f"  Mix: 70% orders (3–5 line items), 20% orders (1 line item), 10% GET /catalog")
    print("-" * 70)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = []

        while time.monotonic() < end_time:
            roll = random.random()
            if roll < 0.7:
                futures.append(pool.submit(send_order, base_url, random.randint(3, 5)))
            elif roll < 0.9:
                futures.append(pool.submit(send_order, base_url, 1))
            else:
                futures.append(pool.submit(send_catalog, base_url))

            total += 1
            time.sleep(interval)

            done = [f for f in futures if f.done()]
            for f in done:
                result = f.result()
                elapsed = result["elapsed_ms"]
                marker = " *** SLO BREACH" if elapsed > 500 else ""
                if elapsed > 500:
                    slow_count += 1
                label = _format_result_row(result)
                print(f"  [{result.get('status', '?')}] {elapsed:>8.1f}ms  {label}{marker}")
                futures.remove(f)

        for f in as_completed(futures):
            result = f.result()
            elapsed = result["elapsed_ms"]
            if elapsed > 500:
                slow_count += 1

    print("-" * 70)
    print(f"Done. Sent {total} requests. {slow_count}/{total} exceeded 500ms SLO ({100*slow_count/max(total,1):.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Load generator for order-service")
    parser.add_argument("base_url", nargs="?", default="http://localhost:8000", help="Base URL of order-service")
    parser.add_argument("--rps", type=float, default=2.0, help="Requests per second (default: 2)")
    parser.add_argument("--duration", type=int, default=120, help="Duration in seconds (default: 120)")
    args = parser.parse_args()

    run_load(args.base_url, args.rps, args.duration)


if __name__ == "__main__":
    main()
