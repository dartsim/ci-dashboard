#!/usr/bin/env python3
"""Collect sanitized GitHub Actions dashboard data for dartsim/dart."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO = os.environ.get("TARGET_REPO", "dartsim/dart")
API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "")
TOKEN = os.environ.get("DASHBOARD_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
OUT_DIR = Path(os.environ.get("DASHBOARD_OUT_DIR", "public"))
REQUIRE_RUNNER_STATUS = os.environ.get("REQUIRE_RUNNER_STATUS", "").lower() in {"1", "true", "yes"}
RUN_LIMIT_ACTIVE = int(os.environ.get("RUN_LIMIT_ACTIVE", "80"))
RUN_LIMIT_COMPLETED = int(os.environ.get("RUN_LIMIT_COMPLETED", "60"))
SYSTEM_RUNNER_LABELS = {"self-hosted", "Linux", "X64", "ARM", "ARM64"}
PRIMARY_LABEL_ORDER = (
    "ubuntu-latest-gpu",
    "ubuntu-latest",
    "macos-latest",
    "windows-latest",
    "self-hosted",
)


warnings: list[str] = []


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def seconds_since(value: str | None, now: datetime) -> int | None:
    dt = parse_time(value)
    if dt is None:
        return None
    return max(0, int((now - dt).total_seconds()))


def seconds_between(start: str | None, end: str | None) -> int | None:
    start_dt = parse_time(start)
    end_dt = parse_time(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0, int((end_dt - start_dt).total_seconds()))


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = math.ceil((pct / 100.0) * len(ordered)) - 1
    index = min(max(index, 0), len(ordered) - 1)
    return ordered[index]


def api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "dartsim-ci-dashboard",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def build_url(path_or_url: str, params: dict[str, Any] | None = None) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    url = f"{API_BASE}{path_or_url}"
    if params:
        url += "?" + urlencode(params)
    return url


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        pieces = part.split(";")
        if len(pieces) < 2:
            continue
        link = pieces[0].strip()
        rel = ";".join(pieces[1:])
        if 'rel="next"' in rel:
            return link[1:-1]
    return None


def get_json(path_or_url: str, params: dict[str, Any] | None = None, *, optional: bool = False) -> tuple[Any | None, dict[str, str]]:
    url = build_url(path_or_url, params)
    request = Request(url, headers=api_headers())
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body), dict(response.headers)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = f"{url} returned HTTP {exc.code}: {body[:240]}"
        if optional:
            warnings.append(message)
            return None, {}
        raise RuntimeError(message) from exc
    except URLError as exc:
        message = f"{url} failed: {exc.reason}"
        if optional:
            warnings.append(message)
            return None, {}
        raise RuntimeError(message) from exc


def get_paginated(path: str, params: dict[str, Any] | None = None, *, optional: bool = False, max_items: int | None = None) -> list[Any]:
    url: str | None = build_url(path, params)
    items: list[Any] = []
    while url:
        payload, headers = get_json(url, optional=optional)
        if payload is None:
            return items
        if isinstance(payload, dict):
            if "workflow_runs" in payload:
                page_items = payload["workflow_runs"]
            elif "runners" in payload:
                page_items = payload["runners"]
            elif "jobs" in payload:
                page_items = payload["jobs"]
            else:
                page_items = [payload]
        else:
            page_items = payload
        items.extend(page_items)
        if max_items is not None and len(items) >= max_items:
            return items[:max_items]
        url = next_link(headers.get("Link"))
    return items


def primary_label(labels: list[str]) -> str:
    label_set = set(labels)
    for label in PRIMARY_LABEL_ORDER:
        if label in label_set:
            return label
    custom = [label for label in labels if label not in SYSTEM_RUNNER_LABELS]
    return ", ".join(custom or labels or ["unlabeled"])


def normalize_job(job: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    labels = list(job.get("labels") or [])
    created_at = job.get("created_at") or run.get("created_at")
    started_at = job.get("started_at")
    completed_at = job.get("completed_at")
    status = job.get("status") or "unknown"
    now = utcnow()
    item = {
        "id": job.get("id"),
        "name": job.get("name"),
        "workflow_name": job.get("workflow_name") or run.get("name"),
        "status": status,
        "conclusion": job.get("conclusion"),
        "labels": labels,
        "primary_label": primary_label(labels),
        "runner_name": job.get("runner_name") or "",
        "runner_group_name": job.get("runner_group_name") or "",
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "queued_seconds": seconds_since(created_at, now) if status == "queued" else seconds_between(created_at, started_at),
        "duration_seconds": seconds_since(started_at, now) if status == "in_progress" else seconds_between(started_at, completed_at),
        "url": job.get("html_url"),
        "run_id": run.get("id"),
        "run_url": run.get("html_url"),
        "run_event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "head_sha": (run.get("head_sha") or "")[:12],
        "display_title": run.get("display_title"),
    }
    return item


def collect_runs(status: str, limit: int) -> list[dict[str, Any]]:
    return get_paginated(
        f"/repos/{REPO}/actions/runs",
        {"per_page": min(limit, 100), "status": status},
        max_items=limit,
    )


def collect_jobs_for_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_jobs: set[int] = set()
    for run in runs:
        run_jobs = get_paginated(
            f"/repos/{REPO}/actions/runs/{run['id']}/jobs",
            {"per_page": 100},
            optional=True,
        )
        for job in run_jobs:
            job_id = job.get("id")
            if job_id in seen_jobs:
                continue
            seen_jobs.add(job_id)
            jobs.append(normalize_job(job, run))
    return jobs


def collect_runners() -> tuple[list[dict[str, Any]], bool]:
    raw = get_paginated(
        f"/repos/{REPO}/actions/runners",
        {"per_page": 100},
        optional=True,
    )
    if not raw:
        return [], False

    runners = []
    for runner in raw:
        labels = [label.get("name") for label in runner.get("labels", []) if label.get("name")]
        runners.append(
            {
                "id": runner.get("id"),
                "name": runner.get("name"),
                "os": runner.get("os"),
                "status": runner.get("status"),
                "busy": bool(runner.get("busy")),
                "labels": labels,
                "idle": runner.get("status") == "online" and not runner.get("busy"),
            }
        )
    return runners, True


def runner_pools(runners: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pools: dict[str, dict[str, Any]] = {}
    for runner in runners:
        for label in runner["labels"]:
            if label in {"self-hosted", "Linux", "X64", "ARM", "ARM64"}:
                continue
            pool = pools.setdefault(
                label,
                {"label": label, "total": 0, "online": 0, "offline": 0, "busy": 0, "idle": 0},
            )
            pool["total"] += 1
            if runner["status"] == "online":
                pool["online"] += 1
                if runner["busy"]:
                    pool["busy"] += 1
                else:
                    pool["idle"] += 1
            else:
                pool["offline"] += 1
    return sorted(pools.values(), key=lambda item: (item["label"] != "ubuntu-latest-gpu", item["label"]))


def labels_match(required: list[str], runner_labels: list[str]) -> bool:
    return set(required).issubset(set(runner_labels))


def build_warnings(queued_jobs: list[dict[str, Any]], runners: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for job in queued_jobs:
        idle_matches = [
            runner["name"]
            for runner in runners
            if runner["idle"] and labels_match(job["labels"], runner["labels"])
        ]
        if idle_matches:
            items.append(
                {
                    "level": "warning",
                    "message": f"{job['name']} is queued for {job['primary_label']} while matching idle runners exist: {', '.join(idle_matches[:4])}",
                    "url": job.get("url") or "",
                }
            )
    return items


def summarize_jobs(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    by_label = Counter(job["primary_label"] for job in jobs)
    return {
        "total": len(jobs),
        "by_label": dict(sorted(by_label.items())),
    }


def recent_stats(completed_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    conclusions = Counter(job.get("conclusion") or "unknown" for job in completed_jobs)
    waits = [job["queued_seconds"] for job in completed_jobs if isinstance(job.get("queued_seconds"), int)]
    durations = [job["duration_seconds"] for job in completed_jobs if isinstance(job.get("duration_seconds"), int)]
    total = len(completed_jobs)
    success = conclusions.get("success", 0)
    return {
        "sample_size": total,
        "conclusions": dict(sorted(conclusions.items())),
        "success_rate": round(success / total, 3) if total else None,
        "queue_seconds": {
            "p50": percentile(waits, 50),
            "p95": percentile(waits, 95),
            "max": max(waits) if waits else None,
        },
        "duration_seconds": {
            "p50": percentile(durations, 50),
            "p95": percentile(durations, 95),
            "max": max(durations) if durations else None,
        },
    }


def latest_gpu_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def is_gpu(job: dict[str, Any]) -> bool:
        name = (job.get("name") or "").lower()
        labels = set(job.get("labels") or [])
        return "ubuntu-latest-gpu" in labels or "cuda" in name or "gpu" in name

    filtered = [job for job in jobs if is_gpu(job)]
    filtered.sort(key=lambda job: job.get("started_at") or job.get("created_at") or "", reverse=True)
    return filtered[:8]


def main() -> int:
    started = time.time()
    now = utcnow()

    active_runs_by_id: dict[int, dict[str, Any]] = {}
    for status in ("queued", "in_progress"):
        for run in collect_runs(status, RUN_LIMIT_ACTIVE):
            active_runs_by_id[run["id"]] = run
    active_jobs = collect_jobs_for_runs(list(active_runs_by_id.values()))

    completed_runs = collect_runs("completed", RUN_LIMIT_COMPLETED)
    completed_jobs = collect_jobs_for_runs(completed_runs)

    queued_jobs = [job for job in active_jobs if job["status"] == "queued"]
    in_progress_jobs = [job for job in active_jobs if job["status"] == "in_progress"]
    queued_jobs.sort(key=lambda job: job.get("queued_seconds") or 0, reverse=True)
    in_progress_jobs.sort(key=lambda job: job.get("duration_seconds") or 0, reverse=True)

    runners, runner_status_available = collect_runners()
    if REQUIRE_RUNNER_STATUS and not runner_status_available:
        raise RuntimeError(
            "Self-hosted runner status is required, but the token could not read "
            f"{REPO} runner metadata. Configure DASHBOARD_GITHUB_TOKEN with Administration: read."
        )

    current_warnings = build_warnings(queued_jobs, runners)
    for warning in warnings:
        current_warnings.append({"level": "notice", "message": warning, "url": ""})
    if not runner_status_available:
        current_warnings.append(
            {
                "level": "notice",
                "message": "Exact self-hosted runner status is unavailable. Add DASHBOARD_GITHUB_TOKEN with Administration: read on dartsim/dart.",
                "url": "https://docs.github.com/en/rest/actions/self-hosted-runners",
            }
        )

    data = {
        "schema_version": 1,
        "generated_at": iso(now),
        "generated_in_seconds": round(time.time() - started, 2),
        "repository": REPO,
        "site_base_url": SITE_BASE_URL,
        "runner_status_available": runner_status_available,
        "summary": {
            "queued_jobs": summarize_jobs(queued_jobs),
            "in_progress_jobs": summarize_jobs(in_progress_jobs),
            "self_hosted_runners": {
                "total": len(runners),
                "online": sum(1 for runner in runners if runner["status"] == "online"),
                "busy": sum(1 for runner in runners if runner["busy"]),
                "idle": sum(1 for runner in runners if runner["idle"]),
                "offline": sum(1 for runner in runners if runner["status"] != "online"),
            },
            "recent": recent_stats(completed_jobs),
        },
        "self_hosted": {
            "runners": sorted(runners, key=lambda item: item["name"] or ""),
            "pools": runner_pools(runners),
        },
        "github_hosted": {
            "availability": "inferred",
            "note": "GitHub-hosted runner capacity is not exposed directly by the Actions API; queue pressure is inferred from queued jobs and recent wait times.",
        },
        "jobs": {
            "queued": queued_jobs[:50],
            "in_progress": in_progress_jobs[:50],
            "latest_gpu": latest_gpu_jobs(active_jobs + completed_jobs),
            "recent_completed": sorted(
                completed_jobs,
                key=lambda job: job.get("completed_at") or "",
                reverse=True,
            )[:50],
        },
        "warnings": current_warnings,
    }

    output = OUT_DIR / "data" / "status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output}")
    print(f"Queued jobs: {len(queued_jobs)}")
    print(f"In-progress jobs: {len(in_progress_jobs)}")
    print(f"Self-hosted runners visible: {len(runners)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"dashboard collection failed: {exc}", file=sys.stderr)
        raise
