#!/usr/bin/env python3
"""Send the N smallest ebooks from c411 to qBittorrent.

Two sources (--source):
  site     (default) c411's own /api/torrents listing, sorted by size server-side.
           Auth: C411_APIKEY (Bearer) and/or C411_COOKIE (copied from the browser).
           Download URL template: C411_DOWNLOAD_URL, default {base}/api/torrents/{hash}/download
           (this route returned 401 with the API key alone; try C411_COOKIE).
  torznab  scan the whole eBooks category via Prowlarr/Torznab and sort locally.

Dry-run by default: pass --go to actually add to qBittorrent.

Config (env vars):
  C411_BASE          default https://c411.org
  C411_APIKEY        c411 API key
  C411_COOKIE        full Cookie header copied from a logged-in browser
  C411_USER_AGENT    must match the browser's UA if the cookie includes cf_clearance
  C411_DOWNLOAD_URL  template with {id} / {hash} placeholders
  C411_CATEGORY      default 2 (Ebook)
  TORZNAB_URL        e.g. http://prowlarr:9696/<indexerId>/api   (--source torznab)
  TORZNAB_APIKEY     Prowlarr API key
  TORZNAB_CAT        default 7020 (eBooks)
  QBIT_URL           e.g. http://localhost:8080
  QBIT_USER / QBIT_PASS
  QBIT_CATEGORY      default "books"
  QBIT_SAVEPATH      optional
"""
import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

# Read settings from .env next to this script (real env vars take precedence)
load_dotenv(Path(__file__).with_name(".env"))

NS = {"torznab": "http://torznab.com/schemas/2015/feed"}


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing env var {name}")
    return v


def fetch_all(url, apikey, cat, page_size, max_pages, delay):
    """Scan a whole Torznab category (no server-side size sort available)."""
    items, seen = [], set()
    for page in range(max_pages):
        r = requests.get(
            url,
            params={"t": "search", "cat": cat, "q": "", "apikey": apikey,
                    "limit": page_size, "offset": page * page_size},
            timeout=60,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        if root.tag == "error":
            sys.exit(f"Torznab error: {root.attrib}")
        new = 0
        for it in root.iter("item"):
            attrs = {a.get("name"): a.get("value") for a in it.findall("torznab:attr", NS)}
            enc = it.find("enclosure")
            link = enc.get("url") if enc is not None else it.findtext("link")
            size = int(attrs.get("size") or (enc.get("length") if enc is not None else 0) or 0)
            guid = it.findtext("guid") or link
            if guid in seen:
                continue
            seen.add(guid)
            new += 1
            items.append({"title": it.findtext("title"), "size": size, "link": link,
                          "hash": (attrs.get("infohash") or "").lower()})
        print(f"page {page}: +{new} (total {len(items)})", file=sys.stderr)
        if new == 0:  # empty page, or indexer ignores offset and repeats page 0
            break
        time.sleep(delay)
    return items


def site_headers():
    # cf_clearance-style cookies are tied to the browser's User-Agent, so allow overriding it
    h = {"User-Agent": env("C411_USER_AGENT", "Mozilla/5.0")}
    if env("C411_APIKEY"):
        h["Authorization"] = f"Bearer {env('C411_APIKEY')}"
    if env("C411_COOKIE"):
        h["Cookie"] = env("C411_COOKIE")
    return h


def fetch_site(count, per_page, delay, headers):
    """Use c411's own listing endpoint, which sorts by size server-side."""
    base = env("C411_BASE", "https://c411.org").rstrip("/")
    tpl = env("C411_DOWNLOAD_URL", f"{base}/api/torrents/{{hash}}/download")
    items, seen, page = [], set(), 1
    while len(items) < count:
        r = requests.get(f"{base}/api/torrents", headers=headers, timeout=60,
                         params={"page": page, "perPage": per_page, "sortBy": "size",
                                 "sortOrder": "asc", "category": env("C411_CATEGORY", "2")})
        try:
            r.raise_for_status()
            data = r.json()["data"]
        except Exception:
            sys.exit(f"Listing failed (HTTP {r.status_code}): {r.text[:300]}")
        if not data:
            break
        for t in data:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            items.append({"title": t["name"], "size": t["size"], "hash": t["infoHash"].lower(),
                          "link": tpl.format(id=t["id"], hash=t["infoHash"])})
        print(f"page {page}: total {len(items)}", file=sys.stderr)
        page += 1
        time.sleep(delay)
    return items


class QBit:
    def __init__(self, url, user, pw):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Referer"] = self.url
        r = self.s.post(f"{self.url}/api/v2/auth/login", data={"username": user, "password": pw})
        # 200 "Ok." on older versions, 204 on newer ones / when auth is bypassed for the subnet
        if not (r.status_code == 204 or r.text.strip() == "Ok."):
            sys.exit(f"qBittorrent login failed: {r.status_code} {r.text}")

    def hashes(self):
        r = self.s.get(f"{self.url}/api/v2/torrents/info")
        r.raise_for_status()
        return {t["hash"].lower() for t in r.json()}

    def add(self, torrent_bytes, category, savepath, paused):
        p = "true" if paused else "false"
        # "paused" for qBittorrent 4.x, "stopped" for 5.x
        data = {"category": category, "paused": p, "stopped": p}
        if savepath:
            data["savepath"] = savepath
        else:
            # Automatic Torrent Management: use the category's save/incomplete paths
            data["autoTMM"] = "true"
        r = self.s.post(f"{self.url}/api/v2/torrents/add", data=data,
                        files={"torrents": ("x.torrent", torrent_bytes, "application/x-bittorrent")})
        # 4.x: 200 "Ok." / 200 "Fails."; 5.x: 2xx on success, 409 if already present
        if r.status_code == 409:
            return "exists"
        if r.ok and r.text.strip() != "Fails.":
            return "added"
        return f"HTTP {r.status_code}: {r.text.strip()[:200]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=10)
    ap.add_argument("--source", choices=["site", "torznab"], default="site",
                    help="site = c411 /api/torrents (sorted server-side), torznab = scan whole category")
    ap.add_argument("--min-size", type=int, default=1, help="ignore items smaller than this many bytes")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=500)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--paused", action="store_true", help="add torrents paused")
    ap.add_argument("--go", action="store_true", help="actually add to qBittorrent (default: dry run)")
    a = ap.parse_args()

    dl_headers = {}
    if a.source == "site":
        dl_headers = site_headers()
        items = fetch_site(a.count, min(a.page_size, 50), a.delay, dl_headers)
    else:
        items = fetch_all(env("TORZNAB_URL", required=True), env("TORZNAB_APIKEY", required=True),
                          env("TORZNAB_CAT", "7020"), a.page_size, a.max_pages, a.delay)
    items = sorted((i for i in items if i["size"] >= a.min_size), key=lambda i: i["size"])[: a.count]

    print(f"\n{len(items)} smallest items:")
    for i in items:
        print(f"{i['size']:>12}  {i['title']}")
    if not a.go:
        print("\nDry run. Re-run with --go to add them to qBittorrent.")
        return

    q = QBit(env("QBIT_URL", required=True), env("QBIT_USER", ""), env("QBIT_PASS", ""))
    existing = q.hashes()
    ok = skipped = failed = 0
    for i in items:
        if i["hash"] and i["hash"] in existing:
            skipped += 1
            continue
        try:
            r = requests.get(i["link"], headers=dl_headers, timeout=60)
            r.raise_for_status()
            if not r.content.startswith(b"d"):  # not a bencoded .torrent
                raise ValueError("response is not a .torrent file")
            res = q.add(r.content, env("QBIT_CATEGORY", "books"), env("QBIT_SAVEPATH"), a.paused)
            if res == "added":
                ok += 1
            elif res == "exists":
                skipped += 1
            else:
                failed += 1
                print(f"qBit rejected {i['title']}: {res}", file=sys.stderr)
        except Exception as e:
            failed += 1
            print(f"FAILED {i['title']}: {e}", file=sys.stderr)
        time.sleep(a.delay)
    print(f"added={ok} skipped(existing)={skipped} failed={failed}")


if __name__ == "__main__":
    main()
