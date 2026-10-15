#!/usr/bin/env python3
"""Netlas harvester for GitHub Actions runners.

Runs on a fresh runner IP (no proxy needed): queries the anonymous Netlas API
for one dork across N pages, then probes every discovered host for an
OpenAI-compatible /v1/models endpoint. Writes a single JSON artifact.

Env:
    DORK   — Netlas query, e.g. 'http.title:"New API"'
    PAGES  — pages per dork (default 3)
    PROBE  — "1" to probe /v1/models (default 1)
"""

import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
API = "https://app.netlas.io/api/responses/"


def netlas_page(query, start, size=100, retries=3):
    url = API + "?" + urllib.parse.urlencode({"q": query, "start": start, "size": size})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read(8_000_000).decode("utf-8", "ignore")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                items = []
                for m in re.finditer(r'"ip":"([^"]+)"[^}]*?"port":(\d+)', raw):
                    items.append({"data": {"ip": m.group(1), "port": int(m.group(2))}})
                return {"items": items}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # runner IP shared/out of budget — wait and retry, or give up
                if attempt < retries - 1:
                    time.sleep(20 * (attempt + 1))
                    continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    return {"items": []}


def harvest(dork, pages):
    hosts = {}
    for page in range(pages):
        try:
            data = netlas_page(dork, page * 100)
        except Exception as exc:
            print(f"[!] page {page} failed: {exc}", file=sys.stderr)
            break
        items = data.get("items", [])
        for it in items:
            dd = it.get("data", {})
            ip, port = dd.get("ip"), dd.get("port")
            if not ip or not port:
                continue
            http = dd.get("http") if isinstance(dd.get("http"), dict) else {}
            geo = dd.get("geoip") if isinstance(dd.get("geoip"), dict) else {}
            hosts[f"{ip}:{port}"] = {
                "ip": ip, "port": port,
                "title": (http.get("title") or "")[:70],
                "isp": (dd.get("isp") or "")[:40],
                "country": geo.get("country", ""),
            }
        print(f"[*] page {page}: +{len(items)} (total {len(hosts)})", flush=True)
        if len(items) < 100:
            break
    return hosts


def probe(host):
    """Check http://ip:port/v1/models for an OpenAI-compatible service."""
    ip, port = host["ip"], host["port"]
    out = dict(host)
    for scheme in ("http", "https"):
        url = f"{scheme}://{ip}:{port}/v1/models"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = resp.read(400_000).decode("utf-8", "ignore")
            if resp.status == 200 and '"data"' in body:
                try:
                    models = [m.get("id") for m in json.loads(body).get("data", [])
                              if isinstance(m, dict) and m.get("id")]
                except json.JSONDecodeError:
                    models = []
                out.update({"probe_scheme": scheme, "openai_ok": True, "models": models[:60]})
                return out
            out.update({"probe_scheme": scheme, "openai_ok": False, "http_status": resp.status})
            return out
        except urllib.error.HTTPError as exc:
            out.update({"probe_scheme": scheme, "openai_ok": False, "http_status": exc.code})
            return out
        except Exception:
            continue
    out.update({"openai_ok": False, "http_status": "unreachable"})
    return out


def main():
    dork = os.environ.get("DORK", 'http.title:"New API"')
    pages = int(os.environ.get("PAGES", "3"))
    do_probe = os.environ.get("PROBE", "1") == "1"

    print(f"[*] dork: {dork!r} pages: {pages}", flush=True)
    hosts = harvest(dork, pages)
    print(f"[*] harvested {len(hosts)} hosts", flush=True)

    if do_probe and hosts:
        results = []
        with concurrent.futures.ThreadPoolExecutor(40) as ex:
            for r in ex.map(probe, list(hosts.values())):
                results.append(r)
        live = [r for r in results if r.get("openai_ok")]
        print(f"[*] probed: {len(results)}, openai-live: {len(live)}", flush=True)
        json.dump({"dork": dork, "harvested": len(results),
                   "live": len(live), "hosts": results},
                  open("out.json", "w"), indent=1)
    else:
        json.dump({"dork": dork, "harvested": len(hosts), "live": 0,
                   "hosts": list(hosts.values())}, open("out.json", "w"), indent=1)

    print("[*] wrote out.json", flush=True)


if __name__ == "__main__":
    main()
