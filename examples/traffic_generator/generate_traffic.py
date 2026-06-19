"""Generate realistic mixed synthetic logs for the toy Java services."""

from __future__ import annotations

import argparse
import random
import string
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml


SERVICES = {
    "user-service": {
        "application": "user-api",
        "methods": [("GET", "/users/42"), ("POST", "/users"), ("GET", "/users/404/profile")],
        "error": ("NullPointerException", 'Cannot invoke "String.toUpperCase()" because "profile" is null', "UserController", "getProfile", "UserController.java", 61),
    },
    "order-service": {
        "application": "order-api",
        "methods": [("POST", "/orders"), ("GET", "/orders/1001"), ("GET", "/orders/user/404")],
        "error": ("NullPointerException", 'Cannot invoke "Customer.getId()" because "customer" is null', "OrderController", "createOrder", "OrderController.java", 87),
    },
    "payment-service": {
        "application": "payment-api",
        "methods": [("POST", "/payments/charge"), ("GET", "/payments/pay-100"), ("POST", "/payments/refund")],
        "error": ("IllegalArgumentException", "amount must be positive", "PaymentController", "charge", "PaymentController.java", 73),
    },
    "inventory-service": {
        "application": "inventory-api",
        "methods": [("GET", "/inventory/SKU-1"), ("POST", "/inventory/reserve"), ("POST", "/inventory/release")],
        "error": ("IllegalStateException", "reserved quantity became negative", "InventoryController", "reserve", "InventoryController.java", 55),
    },
}

MODE_REQUESTS = {"small": 100, "medium": 5000, "large": 100000}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services-config", required=True)
    parser.add_argument("--requests", type=int)
    parser.add_argument("--error-rate", type=float, default=0.15)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--mode", choices=["small", "medium", "large"], default="medium")
    parser.add_argument("--send-http", action="store_true")
    args = parser.parse_args()

    request_count = args.requests or MODE_REQUESTS[args.mode]
    services = load_services(args.services_config)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    handles = {
        service["name"]: (out_dir / f"{service['name']}.log").open("w", encoding="utf-8")
        for service in services
    }
    try:
        for index in range(request_count):
            trace_id = random_id("trc")
            request_id = random_id("req")
            service = services[index % len(services)]
            emit_request(service, handles, trace_id, request_id, args.error_rate, args.send_http)
            if service["name"] == "order-service":
                for downstream in ("user-service", "inventory-service", "payment-service"):
                    downstream_service = next(item for item in services if item["name"] == downstream)
                    emit_request(downstream_service, handles, trace_id, request_id, args.error_rate / 2, args.send_http)
    finally:
        for handle in handles.values():
            handle.close()
    print(f"generated {request_count} synthetic requests under {out_dir}")
    return 0


def load_services(path: str):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    services = raw.get("services", [])
    return [
        {
            "name": service["name"],
            "application": service.get("application_name", SERVICES[service["name"]]["application"]),
            "base_url": service.get("base_url"),
        }
        for service in services
        if service["name"] in SERVICES
    ]


def emit_request(service, handles, trace_id: str, request_id: str, error_rate: float, send_http: bool) -> None:
    profile = SERVICES[service["name"]]
    method, endpoint = random.choice(profile["methods"])
    timestamp = datetime.now(UTC) + timedelta(milliseconds=random.randint(1, 5000))
    duration = random.randint(5, 180)
    roll = random.random()
    if roll < error_rate:
        write_error(handles[service["name"]], timestamp, service, trace_id, request_id, method, endpoint, duration)
    elif roll < error_rate + 0.12:
        write_line(handles[service["name"]], timestamp, "WARN", service, trace_id, request_id, method, endpoint, 400, duration, "validation failed or downstream retry scheduled")
    else:
        write_line(handles[service["name"]], timestamp, "INFO", service, trace_id, request_id, method, endpoint, 200, duration, "request completed")
    if send_http and service.get("base_url"):
        call_http(service["base_url"], method, endpoint)


def write_line(handle, timestamp, level, service, trace_id, request_id, method, endpoint, status, duration, message) -> None:
    handle.write(
        f"{timestamp.isoformat(timespec='milliseconds').replace('+00:00', 'Z')} {level} "
        f"[{service['name']}] [{service['application']}] [traceId={trace_id}] [spanId={random_id('spn')}] "
        f"[requestId={request_id}] [{method} {endpoint}] [status={status}] [durationMs={duration}] "
        f"com.example.{service['name'].split('-')[0]}.{service['application'].split('-')[0].title()}Controller.handle({service['application'].split('-')[0].title()}Controller.java:42) - {message}\n"
    )


def write_error(handle, timestamp, service, trace_id, request_id, method, endpoint, duration) -> None:
    error_type, message, class_name, method_name, file_name, line = SERVICES[service["name"]]["error"]
    package = f"com.example.{service['name'].split('-')[0]}.{class_name}"
    write_line(
        handle,
        timestamp,
        "ERROR",
        service,
        trace_id,
        request_id,
        method,
        endpoint,
        500,
        duration,
        f"java.lang.{error_type}: {message}",
    )
    handle.write(f"    at {package}.{method_name}({file_name}:{line})\n")
    handle.write(f"    at {package.replace('Controller', 'Service')}.submit({file_name.replace('Controller', 'Service')}:54)\n")


def call_http(base_url: str, method: str, endpoint: str) -> None:
    data = b"{}" if method == "POST" else None
    request = urllib.request.Request(base_url.rstrip("/") + endpoint, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(request, timeout=1).read()
    except (urllib.error.URLError, TimeoutError):
        pass


def random_id(prefix: str) -> str:
    return prefix + "-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


if __name__ == "__main__":
    sys.exit(main())
