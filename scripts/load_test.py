#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RequestResult:
    status: int
    latency_seconds: float
    error: str | None = None


@dataclass(frozen=True)
class ResourceSample:
    timestamp: float
    ready_replicas: int
    pod_count: int
    average_cpu_millicores: float | None
    average_memory_mib: float | None


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return math.nan

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * (percent / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)

    if lower == upper:
        return ordered[lower]

    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def request_token(token_url: str, client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()

    request = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Keycloak response did not contain access_token")

    return token


def send_request(url: str, token: str, timeout: float) -> RequestResult:
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
            "User-Agent": "mlops-challenge-load-test/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            status = response.status
            error = None
    except urllib.error.HTTPError as exc:
        exc.read()
        status = exc.code
        error = f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - record all request failures
        status = 0
        error = f"{type(exc).__name__}: {exc}"

    return RequestResult(
        status=status,
        latency_seconds=time.perf_counter() - started,
        error=error,
    )


def parse_cpu_to_millicores(value: str) -> float:
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000
    if value.endswith("u"):
        return float(value[:-1]) / 1_000
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1_000


def parse_memory_to_mib(value: str) -> float:
    suffixes = {
        "Ki": 1 / 1024,
        "Mi": 1,
        "Gi": 1024,
        "Ti": 1024 * 1024,
        "K": 1000 / (1024 * 1024),
        "M": 1_000_000 / (1024 * 1024),
        "G": 1_000_000_000 / (1024 * 1024),
    }

    for suffix, factor in suffixes.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor

    return float(value) / (1024 * 1024)


def kubectl_json(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(completed.stdout)


def collect_sample(namespace: str, app_label: str) -> ResourceSample:
    deployment = kubectl_json(
        ["-n", namespace, "get", "deployment", "loadtester", "-o", "json"]
    )
    ready_replicas = int(deployment.get("status", {}).get("readyReplicas", 0) or 0)

    metrics = kubectl_json(
        [
            "get",
            "--raw",
            f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods",
        ]
    )

    cpu_values: list[float] = []
    memory_values: list[float] = []
    pod_count = 0

    for pod in metrics.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        # Deployment uses app=<name>; also accept the longer recommended label.
        if labels.get("app") != app_label and labels.get("app.kubernetes.io/name") != app_label:
            continue

        pod_count += 1
        pod_cpu = 0.0
        pod_memory = 0.0

        for container in pod.get("containers", []):
            usage = container.get("usage", {})
            pod_cpu += parse_cpu_to_millicores(usage["cpu"])
            pod_memory += parse_memory_to_mib(usage["memory"])

        cpu_values.append(pod_cpu)
        memory_values.append(pod_memory)

    return ResourceSample(
        timestamp=time.time(),
        ready_replicas=ready_replicas,
        pod_count=pod_count,
        average_cpu_millicores=(
            statistics.fmean(cpu_values) if cpu_values else None
        ),
        average_memory_mib=(
            statistics.fmean(memory_values) if memory_values else None
        ),
    )


def monitor_resources(
    stop_event: threading.Event,
    samples: list[ResourceSample],
    namespace: str,
    interval: float,
) -> None:
    while not stop_event.is_set():
        try:
            sample = collect_sample(namespace=namespace, app_label="loadtester")
            samples.append(sample)
            avg_cpu = (
                f"{sample.average_cpu_millicores:.1f}"
                if sample.average_cpu_millicores is not None
                else "n/a"
            )
            avg_mem = (
                f"{sample.average_memory_mib:.1f}"
                if sample.average_memory_mib is not None
                else "n/a"
            )
            print(
                "[metrics] "
                f"ready={sample.ready_replicas} "
                f"pods={sample.pod_count} "
                f"avg_cpu={avg_cpu}m "
                f"avg_memory={avg_mem}Mi",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[metrics] collection failed: {exc}", flush=True)

        stop_event.wait(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticated concurrent load test for the MLOps challenge"
    )
    parser.add_argument(
        "--url",
        default="http://host.k3d.internal:8080/burn",
    )
    parser.add_argument(
        "--token-url",
        default=(
            "http://host.k3d.internal:8080/keycloak/realms/mlops/"
            "protocol/openid-connect/token"
        ),
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("OAUTH2_PROXY_CLIENT_ID", "loadtester"),
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("OAUTH2_PROXY_CLIENT_SECRET"),
    )
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--metrics-interval", type=float, default=5.0)
    parser.add_argument("--namespace", default="loadtester")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.client_secret:
        raise SystemExit(
            "OAUTH2_PROXY_CLIENT_SECRET is required. "
            "Source .challenge_env or pass --client-secret."
        )

    token = request_token(
        token_url=args.token_url,
        client_id=args.client_id,
        client_secret=args.client_secret,
    )

    deadline = time.monotonic() + args.duration
    results: list[RequestResult] = []
    samples: list[ResourceSample] = []
    results_lock = threading.Lock()
    stop_event = threading.Event()

    monitor = threading.Thread(
        target=monitor_resources,
        args=(
            stop_event,
            samples,
            args.namespace,
            args.metrics_interval,
        ),
        daemon=True,
    )
    monitor.start()

    def worker() -> int:
        completed = 0
        while time.monotonic() < deadline:
            result = send_request(
                url=args.url,
                token=token,
                timeout=args.request_timeout,
            )
            with results_lock:
                results.append(result)
            completed += 1
        return completed

    started = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as executor:
        futures = [executor.submit(worker) for _ in range(args.concurrency)]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    elapsed = time.perf_counter() - started
    stop_event.set()
    monitor.join(timeout=args.metrics_interval + 2)

    statuses: dict[int, int] = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1

    # /burn returns 202 when a burn starts and 409 while that pod is already
    # burning (BURN_DURATION default 30s). 409 means load is applied, not auth/
    # infra failure — report it separately from real errors.
    accepted = [result for result in results if result.status == 202]
    busy = [result for result in results if result.status == 409]
    successful = [
        result for result in results if 200 <= result.status < 300
    ]
    failed = [
        result
        for result in results
        if result.status not in (202, 409) and not (200 <= result.status < 300)
    ]
    latencies_ms = [
        result.latency_seconds * 1000 for result in results
    ]

    cpu_samples = [
        sample.average_cpu_millicores
        for sample in samples
        if sample.average_cpu_millicores is not None
    ]
    memory_samples = [
        sample.average_memory_mib
        for sample in samples
        if sample.average_memory_mib is not None
    ]
    ready_replicas = [sample.ready_replicas for sample in samples]

    print("\n=== Load test summary ===")
    print(f"Elapsed:             {elapsed:.2f}s")
    print(f"Requests:            {len(results)}")
    print(f"Accepted (202):      {len(accepted)}")
    print(f"Busy (409):          {len(busy)}")
    print(f"Other 2xx:           {len(successful) - len(accepted)}")
    print(f"Failed:              {len(failed)}")
    print(
        "Error rate:          "
        f"{(len(failed) / len(results) * 100) if results else math.nan:.2f}%"
    )
    print(
        "Busy rate:           "
        f"{(len(busy) / len(results) * 100) if results else math.nan:.2f}%"
    )
    print(
        f"Throughput:          "
        f"{(len(results) / elapsed) if elapsed else math.nan:.2f} req/s"
    )
    print(f"Status distribution: {dict(sorted(statuses.items()))}")

    if latencies_ms:
        print(f"Latency mean:        {statistics.fmean(latencies_ms):.2f} ms")
        print(f"Latency p50:         {percentile(latencies_ms, 50):.2f} ms")
        print(f"Latency p95:         {percentile(latencies_ms, 95):.2f} ms")
        print(f"Latency p99:         {percentile(latencies_ms, 99):.2f} ms")
        print(f"Latency max:         {max(latencies_ms):.2f} ms")

    if ready_replicas:
        print(f"Ready replicas min:  {min(ready_replicas)}")
        print(f"Ready replicas max:  {max(ready_replicas)}")
        print(f"Ready replicas end:  {ready_replicas[-1]}")

    if cpu_samples:
        print(f"Average pod CPU:     {statistics.fmean(cpu_samples):.2f}m")
        print(f"Peak avg pod CPU:    {max(cpu_samples):.2f}m")

    if memory_samples:
        print(f"Average pod memory:  {statistics.fmean(memory_samples):.2f}Mi")

    print(
        "\nNote: 409 'already burning' is expected under concurrency — each "
        "pod accepts one burn at a time (default BURN_DURATION=30s)."
    )

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
