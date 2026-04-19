#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests


CC_BILLING_HEADER = "x-anthropic-billing-header: cc_version=2.1.114.6e1; cc_entrypoint=sdk-cli; cch=00000;"
CC_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
WINDOW_HOURS = 24 * 7
STALE_AFTER_SECONDS = 20 * 60
CONSOLE_REFRESH_SECONDS = 60 * 60
CONSOLE_QUOTA_PER_DOLLAR = 500_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "docs" / "data"
STATUS_PATH = DATA_DIR / "status.json"
HISTORY_PATH = DATA_DIR / "history.json"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def auto_make_url(base: str, path: str) -> str:
    b, p = base.rstrip("/"), path.strip("/")
    if b.endswith("$"):
        return b[:-1].rstrip("/")
    if b.endswith(p):
        return b
    return f"{b}/{p}" if re.search(r"/v\d+(/|$)", b) else f"{b}/v1/{p}"


def build_request_ids() -> Tuple[str, str, str]:
    session_id = str(uuid.uuid4())
    account_uuid = str(uuid.uuid4())
    device_id = uuid.uuid4().hex + uuid.uuid4().hex[:32]
    return session_id, account_uuid, device_id


def build_headers(api_key: str, model: str, session_id: str) -> Tuple[Dict[str, str], str]:
    beta_parts = [
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "redact-thinking-2026-02-12",
        "context-management-2025-06-27",
        "prompt-caching-scope-2026-01-05",
        "advanced-tool-use-2025-11-20",
        "effort-2025-11-24",
    ]
    clean_model = model
    if "[1m]" in model.lower():
        beta_parts.insert(1, "context-1m-2025-08-07")
        clean_model = model.replace("[1m]", "").replace("[1M]", "")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "3000",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.81.0",
        "X-Stainless-OS": "MacOS",
        "X-Stainless-Arch": "arm64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(beta_parts),
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": "claude-cli/2.1.114 (external, sdk-cli)",
        "x-app": "cli",
        "X-Claude-Code-Session-Id": session_id,
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        "authorization": f"Bearer {api_key}",
    }
    return headers, clean_model


def build_common_payload(model: str, prompt: str) -> Dict[str, Any]:
    return {
        "model": model,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
        "context_management": {
            "edits": [
                {
                    "type": "clear_thinking_20251015",
                    "keep": "all",
                }
            ]
        },
        "tools": [],
        "max_tokens": 1,
        "stream": False,
    }


def build_cli_status_payload(
    model: str,
    prompt: str,
    session_id: str,
    account_uuid: str,
    device_id: str,
) -> Dict[str, Any]:
    payload = build_common_payload(model, prompt)
    payload["messages"] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    payload["system"] = [
        {
            "type": "text",
            "text": CC_BILLING_HEADER,
        },
        {
            "type": "text",
            "text": CC_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        },
    ]
    payload["metadata"] = {
        "user_id": json.dumps(
            {
                "device_id": device_id,
                "account_uuid": account_uuid,
                "session_id": session_id,
            },
            separators=(",", ":"),
        )
    }
    return payload


def extract_text(data: Dict[str, Any]) -> str:
    chunks: List[str] = []
    content = data.get("content", [])
    if isinstance(content, str):
        return content.strip()
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            chunks.append(str(block.get("text", "")))

    for choice in data.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            message_content = message.get("content", "")
            if isinstance(message_content, str):
                chunks.append(message_content)
            elif isinstance(message_content, list):
                for block in message_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if isinstance(text, dict):
                            chunks.append(str(text.get("value", "")))
                        else:
                            chunks.append(str(text))
        text = choice.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks).strip()


def response_summary(data: Dict[str, Any]) -> str:
    parts: List[str] = []

    content_types: List[str] = []
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                content_types.append(str(block.get("type") or "unknown"))
    if content_types:
        parts.append(f"content_types={','.join(content_types)}")

    stop_reason = data.get("stop_reason")
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")

    finish_reasons: List[str] = []
    for choice in data.get("choices", []):
        if isinstance(choice, dict):
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                finish_reasons.append(str(finish_reason))
    if finish_reasons:
        parts.append(f"finish_reason={','.join(finish_reasons)}")

    return "; ".join(parts)


def preview_text(text: str, limit: int = 32) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def parse_error_payload(status_code: int, body_text: str) -> Tuple[str, Optional[str]]:
    message = ""
    raw_error_type: Optional[str] = None
    try:
        data = json.loads(body_text)
    except ValueError:
        return (body_text.strip()[:200] or f"HTTP {status_code}", None)

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "").strip()
            raw_error_type = str(err.get("type") or data.get("type") or "").strip() or None
        elif isinstance(err, str):
            message = err.strip()
        if not message:
            message = str(data.get("message") or data.get("detail") or data.get("type") or "").strip()
        if raw_error_type is None:
            raw_error_type = str(data.get("type") or "").strip() or None
    return (message or f"HTTP {status_code}", raw_error_type)


def default_status() -> Dict[str, Any]:
    return {
        "service_name": "Anyrouter Claude CLI Status",
        "overall_status": "no_data",
        "http_status": None,
        "token_ok": False,
        "last_token": "",
        "latency_ms": None,
        "checked_at": None,
        "error_message": "No checks yet",
        "raw_error_type": None,
        "target_model": None,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "console_stats_status": "disabled",
        "console_checked_at": None,
        "console_error_message": "",
        "console_user_quota": None,
        "console_user_used_quota": None,
        "console_user_request_count": None,
        "console_quota_per_dollar": CONSOLE_QUOTA_PER_DOLLAR,
        "console_refresh_seconds": CONSOLE_REFRESH_SECONDS,
    }


def default_history() -> Dict[str, Any]:
    return {
        "generated_at": None,
        "window_hours": WINDOW_HOURS,
        "buckets": [],
    }


def compute_status(checks: int, successes: int) -> str:
    if checks <= 0:
        return "no_data"
    if successes == checks:
        return "operational"
    if successes == 0:
        return "major_outage"
    return "degraded"


def load_json(path: Path, fallback: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return json.loads(json.dumps(fallback))


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def derive_console_base(api_base: str) -> str:
    parsed = urlparse(api_base)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return api_base.rstrip("/")


def solve_acw_sc_v2(challenge_html: str) -> Optional[str]:
    match = re.search(r"<script>([\s\S]*?)</script>", challenge_html, re.IGNORECASE)
    if not match:
        return None

    node_bin = shutil.which("node")
    if not node_bin:
        return None

    with tempfile.TemporaryDirectory(prefix="anyrouter-acw-") as tmpdir:
        script_path = Path(tmpdir) / "challenge.js"
        script_path.write_text(match.group(1), encoding="utf-8")
        runner = f"""
const fs = require("fs");
const vm = require("vm");
const script = fs.readFileSync({json.dumps(str(script_path))}, "utf8");
let cookie = "";
const sandbox = {{
  document: {{
    set cookie(v) {{ cookie = v; }},
    get cookie() {{ return cookie; }},
    location: {{ reload() {{}} }},
  }},
  Date,
  RegExp,
  parseInt,
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
}};
vm.createContext(sandbox);
vm.runInContext(script, sandbox, {{ timeout: 5000 }});
process.stdout.write(cookie);
"""
        try:
            output = subprocess.check_output([node_bin, "-e", runner], text=True, timeout=10)
        except Exception:
            return None

    cookie_match = re.search(r"acw_sc__v2=([^;]+)", output)
    return cookie_match.group(1) if cookie_match else None


def fetch_console_user_self(console_base: str, session_cookie: str, user_id: int, timeout: int) -> Dict[str, Any]:
    result = {
        "console_stats_status": "error",
        "console_checked_at": iso_z(utc_now()),
        "console_error_message": "",
        "console_user_quota": None,
        "console_user_used_quota": None,
        "console_user_request_count": None,
    }

    common_headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "application/json, text/plain, */*",
    }
    session = requests.Session()
    session.headers.update(common_headers)
    session.cookies.set("session", session_cookie, domain=urlparse(console_base).hostname or "", path="/")
    try:
        challenge_response = session.get(f"{console_base.rstrip('/')}/console", timeout=timeout)
        challenge_html = challenge_response.text
    except Exception as exc:
        result["console_error_message"] = f"Console bootstrap failed: {type(exc).__name__}: {exc}"
        return result

    acw_cookie = solve_acw_sc_v2(challenge_html)
    if acw_cookie:
        session.cookies.set("acw_sc__v2", acw_cookie, domain=urlparse(console_base).hostname or "", path="/")
    try:
        session.get(f"{console_base.rstrip('/')}/console", timeout=timeout)
    except Exception:
        pass
    try:
        response = session.get(
            f"{console_base.rstrip('/')}/api/user/self",
            headers={"New-API-User": str(user_id)},
            timeout=timeout,
        )
        body_text = response.text
    except Exception as exc:
        result["console_error_message"] = f"Console API failed: {type(exc).__name__}: {exc}"
        return result

    if response.status_code != 200:
        message, _ = parse_error_payload(response.status_code, body_text)
        result["console_error_message"] = message
        return result

    try:
        payload = json.loads(body_text)
    except ValueError as exc:
        result["console_error_message"] = f"Console JSON invalid: {exc}"
        return result

    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        result["console_error_message"] = str(payload.get("message") or "Console API returned no data")
        return result

    data = payload["data"]
    result["console_stats_status"] = "ok"
    result["console_user_quota"] = data.get("quota")
    result["console_user_used_quota"] = data.get("used_quota")
    result["console_user_request_count"] = data.get("request_count")
    result["console_error_message"] = ""
    return result


def enrich_console_stats(
    snapshot: Dict[str, Any],
    console_base: str,
    session_cookie: str,
    user_id: int,
    timeout: int,
) -> Dict[str, Any]:
    stats = fetch_console_user_self(console_base, session_cookie, user_id, timeout)
    snapshot.update(stats)
    snapshot["console_quota_per_dollar"] = CONSOLE_QUOTA_PER_DOLLAR
    snapshot["console_refresh_seconds"] = CONSOLE_REFRESH_SECONDS
    return snapshot


def should_refresh_console(previous_status: Dict[str, Any], now_iso: str) -> bool:
    previous_checked_at = previous_status.get("console_checked_at")
    if not previous_checked_at:
        return True
    try:
        previous_dt = datetime.fromisoformat(str(previous_checked_at).replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except Exception:
        return True
    return (now_dt - previous_dt).total_seconds() >= CONSOLE_REFRESH_SECONDS


def carry_console_stats(snapshot: Dict[str, Any], previous_status: Dict[str, Any]) -> Dict[str, Any]:
    for key in [
        "console_stats_status",
        "console_checked_at",
        "console_error_message",
        "console_user_quota",
        "console_user_used_quota",
        "console_user_request_count",
        "console_quota_per_dollar",
    ]:
        snapshot[key] = previous_status.get(key, snapshot.get(key))
    snapshot["console_refresh_seconds"] = CONSOLE_REFRESH_SECONDS
    return snapshot


def merge_history(history: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.fromisoformat(snapshot["checked_at"].replace("Z", "+00:00"))
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    cutoff = current_hour - timedelta(hours=WINDOW_HOURS - 1)

    raw_buckets = history.get("buckets", [])
    buckets: Dict[str, Dict[str, Any]] = {}
    for bucket in raw_buckets:
        try:
            bucket_dt = datetime.fromisoformat(bucket["hour"].replace("Z", "+00:00"))
        except Exception:
            continue
        if bucket_dt < cutoff:
            continue
        checks = int(bucket.get("checks", 0))
        successes = int(bucket.get("successes", 0))
        avg_latency_ms = bucket.get("avg_latency_ms")
        buckets[iso_z(bucket_dt)] = {
            "hour": iso_z(bucket_dt),
            "checks": checks,
            "successes": successes,
            "last_http_status": bucket.get("last_http_status"),
            "avg_latency_ms": avg_latency_ms,
            "last_error_message": bucket.get("last_error_message"),
            "status": compute_status(checks, successes),
        }

    hour_key = iso_z(current_hour)
    bucket = buckets.get(
        hour_key,
        {
            "hour": hour_key,
            "checks": 0,
            "successes": 0,
            "last_http_status": None,
            "avg_latency_ms": None,
            "last_error_message": "",
            "status": "no_data",
        },
    )

    prev_checks = int(bucket["checks"])
    latency_ms = snapshot.get("latency_ms")
    bucket["checks"] = prev_checks + 1
    if snapshot.get("token_ok"):
        bucket["successes"] = int(bucket["successes"]) + 1
    if latency_ms is not None:
        previous_avg = bucket.get("avg_latency_ms")
        if previous_avg is None or prev_checks <= 0:
            bucket["avg_latency_ms"] = latency_ms
        else:
            bucket["avg_latency_ms"] = round(((previous_avg * prev_checks) + latency_ms) / (prev_checks + 1), 2)
    bucket["last_http_status"] = snapshot.get("http_status")
    bucket["last_error_message"] = snapshot.get("error_message", "")
    bucket["status"] = compute_status(int(bucket["checks"]), int(bucket["successes"]))
    buckets[hour_key] = bucket

    ordered = [buckets[key] for key in sorted(buckets.keys()) if datetime.fromisoformat(key.replace("Z", "+00:00")) >= cutoff]
    return {
        "generated_at": snapshot["checked_at"],
        "window_hours": WINDOW_HOURS,
        "buckets": ordered[-WINDOW_HOURS:],
    }


def execute_probe(
    api_base: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    result: Dict[str, Any],
    timeout: int,
) -> Dict[str, Any]:
    started = time.monotonic()
    url = auto_make_url(api_base, "messages") + "?beta=true"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result["http_status"] = response.getcode()
            body_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        body_text = exc.read().decode("utf-8", errors="replace")
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        error_message, raw_error_type = parse_error_payload(exc.code, body_text)
        result["overall_status"] = "major_outage"
        result["error_message"] = error_message
        result["raw_error_type"] = raw_error_type
        return result
    except urllib.error.URLError as exc:
        result["overall_status"] = "major_outage"
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["error_message"] = f"{type(exc.reason).__name__}: {exc.reason}"
        return result
    except Exception as exc:
        result["overall_status"] = "major_outage"
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["error_message"] = f"{type(exc).__name__}: {exc}"
        return result

    result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)

    if result["http_status"] != 200:
        error_message, raw_error_type = parse_error_payload(int(result["http_status"]), body_text)
        result["overall_status"] = "major_outage"
        result["error_message"] = error_message
        result["raw_error_type"] = raw_error_type
        return result

    try:
        data = json.loads(body_text)
    except ValueError as exc:
        result["overall_status"] = "degraded"
        result["error_message"] = f"Invalid JSON: {exc}"
        return result

    text = extract_text(data)
    if text:
        result["overall_status"] = "operational"
        result["token_ok"] = True
        result["last_token"] = preview_text(text)
        result["error_message"] = ""
        return result

    result["overall_status"] = "degraded"
    summary = response_summary(data)
    if summary:
        result["error_message"] = f"No text content in response ({summary})"
    else:
        result["error_message"] = "No text content in response"
    return result


def run_probe(
    api_base: str,
    api_key: str,
    model: str,
    timeout: int,
    prompt: str,
    console_base: str,
    console_session: str,
    console_user_id: Optional[int],
    previous_status: Dict[str, Any],
) -> Dict[str, Any]:
    checked_at = iso_z(utc_now())
    status = default_status()
    status["checked_at"] = checked_at
    status["target_model"] = model

    session_id, account_uuid, device_id = build_request_ids()
    headers, clean_model = build_headers(api_key, model, session_id)
    snapshot = execute_probe(
        api_base,
        headers,
        build_cli_status_payload(clean_model, prompt, session_id, account_uuid, device_id),
        status,
        timeout,
    )

    if console_session and console_user_id is not None:
        if not should_refresh_console(previous_status, snapshot["checked_at"]):
            return carry_console_stats(snapshot, previous_status)
        return enrich_console_stats(
            snapshot,
            console_base,
            console_session,
            console_user_id,
            timeout,
        )

    snapshot["console_stats_status"] = "disabled"
    snapshot["console_checked_at"] = snapshot["checked_at"]
    snapshot["console_quota_per_dollar"] = CONSOLE_QUOTA_PER_DOLLAR
    snapshot["console_refresh_seconds"] = CONSOLE_REFRESH_SECONDS
    return snapshot


def parse_args() -> argparse.Namespace:
    load_dotenv(PROJECT_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Probe anyrouter and refresh status page data.")
    parser.add_argument("--api-base", default=os.environ.get("ANYROUTER_API_BASE", ""), help="Anyrouter base URL")
    parser.add_argument("--api-key", default=os.environ.get("ANYROUTER_API_KEY", ""), help="Anyrouter API key")
    parser.add_argument(
        "--model",
        default=os.environ.get("ANYROUTER_MODEL", "claude-opus-4-7[1m]"),
        help="Model name",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("ANYROUTER_TIMEOUT", "30")),
        help="Read timeout in seconds",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly one visible character: A",
        help="Probe prompt",
    )
    parser.add_argument(
        "--console-base",
        default=os.environ.get("ANYROUTER_CONSOLE_BASE", derive_console_base(os.environ.get("ANYROUTER_API_BASE", ""))),
        help="Anyrouter console origin, e.g. https://anyrouter.top",
    )
    parser.add_argument(
        "--console-session",
        default=os.environ.get("ANYROUTER_CONSOLE_SESSION", ""),
        help="Anyrouter console session cookie value",
    )
    parser.add_argument(
        "--console-user-id",
        type=int,
        default=int(os.environ["ANYROUTER_CONSOLE_USER_ID"]) if os.environ.get("ANYROUTER_CONSOLE_USER_ID") else None,
        help="Anyrouter console localStorage.user.id",
    )
    parser.add_argument("--status-path", default=str(STATUS_PATH), help="status.json output path")
    parser.add_argument("--history-path", default=str(HISTORY_PATH), help="history.json output path")
    parser.add_argument("--print-json", action="store_true", help="Print the current snapshot to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_base:
        print("Missing ANYROUTER_API_BASE", file=sys.stderr)
        return 2
    if not args.api_key:
        print("Missing ANYROUTER_API_KEY", file=sys.stderr)
        return 2

    previous_status = load_json(Path(args.status_path), default_status())
    snapshot = run_probe(
        args.api_base,
        args.api_key,
        args.model,
        args.timeout,
        args.prompt,
        args.console_base,
        args.console_session,
        args.console_user_id,
        previous_status,
    )
    history = load_json(Path(args.history_path), default_history())
    merged_history = merge_history(history, snapshot)

    try:
        write_json(Path(args.status_path), snapshot)
        write_json(Path(args.history_path), merged_history)
    except OSError as exc:
        print(f"Failed to write data files: {exc}", file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                {
                    "overall_status": snapshot["overall_status"],
                    "http_status": snapshot["http_status"],
                    "token_ok": snapshot["token_ok"],
                    "checked_at": snapshot["checked_at"],
                    "error_message": snapshot["error_message"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
