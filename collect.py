#!/usr/bin/env python3
"""Dispatch the netlas-harvest workflow, wait, download artifacts, merge.

Usage:
    python3 collect.py [--pages 3] [--probe 1] [--out merged.json] [--wait]
    python3 collect.py --run-id 12345678     # collect an existing run
"""

import argparse
import concurrent.futures
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = "bokir-tampan/netlas-harvest"
WORKFLOW = "harvest.yml"
API = "https://api.github.com"
TOKEN_FILE = Path("/root/.hermes/credentials/github-token.txt")
HERE = Path(__file__).parent


def token():
    t = os.environ.get("GITHUB_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    if not t:
        sys.exit("[!] no GitHub token (env GITHUB_TOKEN or " + str(TOKEN_FILE) + ")")
    return t


def api(path, method="GET", body=None, raw=False, token_override=None):
    url = path if path.startswith("http") else API + path
    headers = {"Authorization": f"token {token_override or token()}",
               "User-Agent": "elyy-agent", "Accept": "application/vnd.github+json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = resp.read()
    return payload if raw else (json.loads(payload) if payload else None)


def dispatch(pages, probe):
    api(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches", "POST",
        {"ref": "main", "inputs": {"pages": str(pages), "probe": str(probe)}})
    print("[*] dispatched", flush=True)
    time.sleep(8)
    runs = api(f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=1")
    return runs["workflow_runs"][0]["id"]


def wait_run(run_id, timeout_s=900):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        run = api(f"/repos/{REPO}/actions/runs/{run_id}")
        status, concl = run["status"], run.get("conclusion")
        done = sum(1 for j in api(f"/repos/{REPO}/actions/runs/{run_id}/jobs")["jobs"]
                   if j["status"] == "completed")
        total = run.get("run_attempt") and len(api(f"/repos/{REPO}/actions/runs/{run_id}/jobs")["jobs"])
        print(f"    {status}/{concl or '-'} jobs done: {done}/{total} ({time.time()-t0:.0f}s)", flush=True)
        if status == "completed":
            return run
        time.sleep(20)
    raise TimeoutError("run did not finish in time")


def collect(run_id, out_path):
    arts = api(f"/repos/{REPO}/actions/runs/{run_id}/artifacts")["artifacts"]
    print(f"[*] {len(arts)} artifacts", flush=True)
    hosts, live, dorks = {}, [], set()

    def pull(art):
        blob = api(art["archive_download_url"], raw=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    return json.loads(zf.read(name).decode())
        return None

    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for data in ex.map(pull, arts):
            if not data:
                continue
            dorks.add(data.get("dork", ""))
            for h in data.get("hosts", []):
                key = f"{h.get('ip')}:{h.get('port')}"
                prev = hosts.get(key)
                if prev and prev.get("openai_ok") and not h.get("openai_ok"):
                    continue
                hosts[key] = h
                if h.get("openai_ok"):
                    live.append(h)

    merged = {"run_id": run_id, "dorks": sorted(dorks),
              "harvested": len(hosts), "live_count": len(live),
              "live": live, "hosts": list(hosts.values())}
    Path(out_path).write_text(json.dumps(merged, indent=1))
    print(f"[*] {len(hosts)} hosts, {len(live)} with live /v1/models -> {out_path}", flush=True)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default=3)
    ap.add_argument("--probe", default=1)
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--out", default=str(HERE / "merged.json"))
    ap.add_argument("--wait", action="store_true")
    args = ap.parse_args()

    run_id = args.run_id or dispatch(args.pages, args.probe)
    print(f"[*] run id {run_id}", flush=True)
    wait_run(run_id)
    merged = collect(run_id, args.out)
    for h in merged["live"][:25]:
        print(f"  LIVE {h['ip']}:{h['port']} [{h.get('country','')}] "
              f"{','.join(h.get('models', [])[:3])}", flush=True)


if __name__ == "__main__":
    main()
