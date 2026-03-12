#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.request


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mark proposal feedback for learning")
    p.add_argument("--url", required=True, help="Lead URL")
    p.add_argument("--verdict", required=True, choices=["good", "bad", "neutral"])
    p.add_argument("--note", default="")
    p.add_argument("--api", default="http://127.0.0.1:8000")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    payload = json.dumps({"lead_url": args.url, "verdict": args.verdict, "note": args.note}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.api.rstrip('/')}/feedback",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
