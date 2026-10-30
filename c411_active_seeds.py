#!/usr/bin/env python3
"""Export your c411 active seeds to a CSV file.

Reads /api/profile/active-seeds page by page (the server caps perPage at 50) and writes
one row per torrent: name, size (bytes and human readable), seedingTime (seconds and
days/hours/minutes), seeders, uploaded (bytes and human readable), category slug, zenPerDay.

zenPerDay is the torrent's share of your last daily Zen payout, as computed by c411
(zenByTorrent from /api/profile/releases-vtr). It is left empty when c411 has no value
for the torrent yet; the site shows an estimate there, which this script doesn't compute.

Needs the same C411_COOKIE / C411_USER_AGENT config as c411_smallest_torrents.py.
"""

import argparse
import csv
import os
import sys
import time

import requests

from c411_smallest_torrents import env, human_size, site_headers

COLUMNS = [
    "name",
    "size",
    "sizeHuman",
    "seedingTime",
    "seedingTimeHuman",
    "seeders",
    "uploaded",
    "uploadedHuman",
    "category",
    "zenPerDay",
]


def human_duration(seconds):
    """Seconds -> "32d 11h 54m" (seconds dropped)."""
    m = seconds // 60
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h:02}h {m:02}m"


def get_json(path, headers, params=None):
    base = env("C411_BASE", "https://c411.org").rstrip("/")
    r = requests.get(f"{base}{path}", headers=headers, timeout=60, params=params)
    try:
        r.raise_for_status()
        return r.json()
    except Exception:
        sys.exit(f"{path} failed (HTTP {r.status_code}): {r.text[:300]}")


def fetch_zen(headers):
    """torrentId (str) -> Zen per day, from the last daily payout."""
    body = get_json("/api/profile/releases-vtr", headers)
    print(
        f"Zen payout of {body.get('attributionDate')}: {len(body['zenByTorrent'])} torrents",
        file=sys.stderr,
    )
    return body["zenByTorrent"]


def fetch_seeds(per_page, delay, headers, zen):
    rows, page, total_pages = [], 1, 1
    while page <= total_pages:
        body = get_json(
            "/api/profile/active-seeds", headers, {"page": page, "perPage": per_page}
        )
        total_pages = body["meta"]["totalPages"]
        for t in body["data"]:
            size, seeding, up = (
                int(t["size"]),
                int(t["seedingTime"]),
                int(t["uploaded"]),
            )
            rows.append(
                {
                    "name": t["name"],
                    "size": size,
                    "sizeHuman": human_size(size),
                    "seedingTime": seeding,
                    "seedingTimeHuman": human_duration(seeding),
                    "seeders": t["seeders"],
                    "uploaded": up,
                    "uploadedHuman": human_size(up),
                    "category": t["category"]["slug"],
                    "zenPerDay": zen.get(str(t["torrentId"]), ""),
                }
            )
        print(f"page {page}/{total_pages}: total {len(rows)}", file=sys.stderr)
        page += 1
        if page <= total_pages:
            time.sleep(delay)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-o", "--output", default="output/active_seeds.csv", help="CSV file to write"
    )
    ap.add_argument(
        "--page-size", type=int, default=50, help="the server caps this at 50"
    )
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    a = ap.parse_args()

    headers = site_headers()
    zen = fetch_zen(headers)
    time.sleep(a.delay)
    rows = fetch_seeds(a.page_size, a.delay, headers, zen)
    os.makedirs(os.path.dirname(a.output), exist_ok=True)
    with open(a.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    total = sum(r["zenPerDay"] for r in rows if r["zenPerDay"] != "")
    missing = sum(1 for r in rows if r["zenPerDay"] == "")
    print(
        f"Wrote {len(rows)} seeds to {a.output} ({total:.2f} Zen/day, {missing} without a value)"
    )


if __name__ == "__main__":
    main()
