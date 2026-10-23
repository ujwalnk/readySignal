"""Slack Block Kit rendering and curl-based delivery."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import duration_text, parse_iso

PLACEHOLDERS = ("prompt", "project", "account", "icon", "ts_expired", "ts_start", "duration", "reset_count")
_TOKEN = re.compile(r"\$\$|\$\{(\w+)\}|\$(\w+)")
MAX_BLOCKS = 50
MAX_PAYLOAD_BYTES = 100_000
PERMANENT_STATUSES = {400, 403, 404, 410}  # retrying cannot help
MAX_RETRY_AFTER = 3600


# ---------------------------------------------------------------- templates
def expand(text: str, ctx: dict[str, str]) -> str:
    """Single-pass expansion: ``$$`` -> ``$``; known names substituted; unknown left verbatim."""

    def repl(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(2)
        return ctx[name] if name in ctx else m.group(0)

    return _TOKEN.sub(repl, text)


def _walk(node: Any, ctx: dict[str, str]) -> Any:
    if isinstance(node, str):
        return expand(node, ctx)
    if isinstance(node, list):
        return [_walk(x, ctx) for x in node]
    if isinstance(node, dict):
        return {k: _walk(v, ctx) for k, v in node.items()}  # keys are never expanded
    return node


def parse_template(template: str) -> dict[str, Any]:
    try:
        data = json.loads(template)
    except ValueError as exc:
        raise ValueError(f"template is not valid JSON: {exc}") from exc
    validate_payload(data)
    return data


def validate_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    text, blocks = payload.get("text"), payload.get("blocks")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("payload needs a non-empty fallback 'text' string")
    if blocks is not None:
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("'blocks' must be a non-empty array")
        if len(blocks) > MAX_BLOCKS:
            raise ValueError(f"Slack allows at most {MAX_BLOCKS} blocks")
        for i, b in enumerate(blocks):
            if not isinstance(b, dict) or not isinstance(b.get("type"), str):
                raise ValueError(f"block {i} must be an object with a string 'type'")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large")


def render(template: str, ctx: dict[str, str]) -> dict[str, Any]:
    payload = _walk(parse_template(template), ctx)
    validate_payload(payload)
    return payload


def format_ts(value: str | None, tz_name: str, fmt: str) -> str:
    if not value:
        return "—"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return parse_iso(value).astimezone(tz).strftime(fmt)


def validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unknown time zone '{name}'") from exc


def validate_ts_format(fmt: str) -> None:
    if not fmt or len(fmt) > 64:
        raise ValueError("timestamp format must be 1-64 characters")
    try:
        datetime(2026, 1, 2, 3, 4, 5).strftime(fmt)
    except ValueError as exc:
        raise ValueError("invalid strftime format") from exc


def build_context(snap: dict[str, Any], cfg: dict[str, Any]) -> dict[str, str]:
    tz = cfg["timezone"]
    return {
        "prompt": snap.get("prompt") or "—",
        "project": snap.get("project") or "—",
        "account": snap["account"],
        "icon": snap.get("icon", ""),
        "ts_start": format_ts(snap.get("ts_start"), tz, cfg["ts_start_format"]),
        "ts_expired": format_ts(snap.get("ts_expired"), tz, cfg["ts_expired_format"]),
        "duration": duration_text(snap.get("duration_seconds", 0)),
        "reset_count": str(snap.get("reset_count", 0)),
    }


def reminder_payload(snap: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Fixed Block Kit message for a one-off reminder (not the cooldown-complete template)."""
    when = format_ts(snap.get("ts_expired"), cfg["timezone"], cfg["ts_expired_format"])
    title = f"{snap.get('icon', '')} Reminder: {snap['account']}".strip()
    lines = [f"*{title}*", f"Scheduled for {when}"]
    if snap.get("project"):
        lines.append(f"Project: {snap['project']}")
    payload = {"text": title, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]}
    validate_payload(payload)
    return payload


SAMPLE_SNAPSHOT = {
    "account": "Example — Work",
    "icon": "🤖",
    "project": 'Quarterly "report" \\ draft',
    "prompt": "Continue with section 3.\nKeep the tone formal.",
    "ts_start": "2026-10-15T08:00:00Z",
    "ts_expired": "2026-10-15T13:00:00Z",
    "duration_seconds": 18000,
    "reset_count": 2,
}


# ------------------------------------------------------------------- curl
@dataclass
class CurlResult:
    exit_code: int = 0
    status: int | None = None
    body: str = ""
    retry_after: int | None = None
    error: str | None = None


@dataclass
class Outcome:
    ok: bool
    status: int | None = None
    error: str | None = None
    permanent: bool = False
    retry_after: int | None = None


def sanitize(text: str, secrets: tuple[str, ...] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[webhook]")
    text = re.sub(r"https?://\S+", "[url]", text)
    return " ".join(text.split())[:200]


def _parse_http(raw: str) -> tuple[int | None, dict[str, str], str]:
    status: int | None = None
    headers: dict[str, str] = {}
    while raw.startswith("HTTP/"):
        head, sep, rest = raw.partition("\r\n\r\n")
        if not sep:
            head, sep, rest = raw.partition("\n\n")
        lines = head.splitlines()
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = None
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        raw = rest
    return status, headers, raw


async def curl_post(url: str, body: bytes, connect_timeout: int, total_timeout: int) -> CurlResult:
    """POST via curl without a shell. The URL travels over an inherited pipe (``-K /dev/fd/N``), the JSON over stdin;
    neither appears in argv. Residual exposure: root / same-uid processes can read /proc/<pid>/fd and memory."""
    if any(c in url for c in '"\\\r\n \t'):
        return CurlResult(exit_code=-1, error="webhook URL contains illegal characters")
    rfd, wfd = os.pipe()
    try:
        os.write(wfd, f'url = "{url}"\n'.encode())
        os.close(wfd)
        wfd = -1
        args = [
            "curl",
            "--silent",
            "--show-error",
            "--include",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--connect-timeout",
            str(connect_timeout),
            "--max-time",
            str(total_timeout),
            "-X",
            "POST",
            "-H",
            "Content-Type: application/json; charset=utf-8",
            "--data-binary",
            "@-",
            "-K",
            f"/dev/fd/{rfd}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(rfd,),
            )
        except FileNotFoundError:
            return CurlResult(exit_code=127, error="curl executable not found")
        except OSError as exc:
            return CurlResult(exit_code=-1, error=f"could not start curl: {exc.strerror}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(body), timeout=total_timeout + 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return CurlResult(exit_code=-1, error="curl subprocess timed out")
        code = proc.returncode or 0
        status, headers, text = _parse_http(out.decode("utf-8", "replace"))
        ra = headers.get("retry-after", "")
        return CurlResult(
            code,
            status,
            text.strip(),
            int(ra) if ra.isdigit() else None,
            sanitize(err.decode("utf-8", "replace"), (url,)) if code else None,
        )
    finally:
        for fd in (rfd, wfd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


async def send_slack(url: str, payload: dict[str, Any], connect_timeout: int, total_timeout: int) -> Outcome:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    res = await curl_post(url, body, connect_timeout, total_timeout)
    if res.exit_code != 0 or res.status is None:
        return Outcome(False, res.status, f"curl failed (exit {res.exit_code}): {res.error or 'no response'}")
    if res.status == 200 and res.body == "ok":
        return Outcome(True, 200)
    snippet = sanitize(res.body, (url,))
    if res.status == 200:
        return Outcome(False, 200, f"unexpected response body: {snippet}")
    retry_after = min(res.retry_after, MAX_RETRY_AFTER) if res.status == 429 and res.retry_after else None
    return Outcome(
        False,
        res.status,
        f"HTTP {res.status}: {snippet}",
        permanent=res.status in PERMANENT_STATUSES,
        retry_after=retry_after,
    )


def validate_webhook_url(url: str) -> None:
    if not re.fullmatch(r"https://[^\s\"'\\]{4,500}", url):
        raise ValueError("webhook URL must be an https:// URL without spaces or quotes")


def mask_url(url: str) -> str:
    if not url:
        return ""
    host = re.sub(r"^https://([^/]+).*$", r"\1", url)
    return f"https://{host}/…{url[-4:]}"
