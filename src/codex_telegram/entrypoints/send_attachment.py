"""Queue outbound attachment deliveries through the admin API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error, request


def enqueue_attachment_job(
    base_url: str,
    admin_token: str,
    *,
    thread_id: str,
    path: str,
    caption: str | None = None,
) -> dict[str, object]:
    """Queue one attachment job via the local admin API."""
    payload: dict[str, object] = {
        "thread_id": thread_id,
        "path": path,
    }
    if caption is not None:
        payload["caption"] = caption
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        base_url.rstrip("/") + "/attachments",
        data=body,
        headers={
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Attachment API returned {exc.code}: {detail}") from exc
    if not raw:
        return {}
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise SystemExit("Attachment API returned a non-object response.")
    return result


def main() -> int:
    """Queue one attachment delivery request for the running bot."""
    parser = argparse.ArgumentParser(prog="codex-telegram-send-attachment")
    parser.add_argument("--thread-id", required=True, help="Bridge id")
    parser.add_argument("--caption", default=None, help="Optional Telegram caption")
    parser.add_argument("path", help="Absolute file path under /attachments")
    args = parser.parse_args()

    base_url = os.environ.get(
        "CODEX_TELEGRAM_ATTACHMENT_ADMIN_URL",
        "http://127.0.0.1:19080",
    )
    admin_token = os.environ.get("CODEX_TELEGRAM_ATTACHMENT_ADMIN_TOKEN")
    if not admin_token:
        raise SystemExit("CODEX_TELEGRAM_ATTACHMENT_ADMIN_TOKEN is required")
    result = enqueue_attachment_job(
        base_url,
        admin_token,
        thread_id=args.thread_id,
        path=args.path,
        caption=args.caption,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
