#!/usr/bin/env python3
"""Sweep shard for port 3000 + HTTP verify /v1/models + /register."""
import json, sys, socket, ssl, time, os, signal
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

SHARD = os.environ.get("SHARD", "shard-0")
PORT = 3000
CONCURRENCY = 500
TCP_TIMEOUT = 1.2
HTTP_TIMEOUT = 6
# hard wall-clock cap per host for the whole 3-request probe
HOST_BUDGET = 20
socket.setdefaulttimeout(HTTP_TIMEOUT)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"}
ssl._create_default_https_context = ssl._create_unverified_context

def tcp_check(ip, port=PORT):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(TCP_TIMEOUT)
        r = s.connect_ex((ip, port))
        s.close()
        return ip if r == 0 else None
    except:
        return None

def http_get_bounded(ip, port, path, read_bytes=10000, budget=HOST_BUDGET):
    """Raw socket GET with hard wall-clock budget. Returns (status, body) or ('ERR', None)."""
    import select
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TCP_TIMEOUT)
    try:
        s.connect((ip, port))
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {ip}:{port}\r\n"
                   f"User-Agent: Mozilla/5.0\r\nConnection: close\r\nAccept: */*\r\n\r\n").encode())
        buf = b""
        while len(buf) < read_bytes:
            remaining = budget - (time.time() - t0)
            if remaining <= 0: return ("TIMEOUT", None)
            r, _, _ = select.select([s], [], [], min(remaining, 2.0))
            if not r:
                if b"\r\n\r\n" in buf: break  # headers done, body optional
                continue
            chunk = s.recv(8192)
            if not chunk: break
            buf += chunk
            if b"\r\n\r\n" in buf and len(buf) > read_bytes: break
        if not buf: return ("EMPTY", None)
        head, _, body = buf.partition(b"\r\n\r\n")
        status = 0
        try: status = int(head.split(b" ")[1])
        except: pass
        return (status, body[:read_bytes].decode("utf-8", "ignore"))
    except Exception as e:
        return ("ERR", None)
    finally:
        s.close()

def http_probe(ip, port=PORT):
    result = {"ip": ip, "port": port, "models": None, "register": None, "panel": None, "chat_401": False}
    # /v1/models
    st, body = http_get_bounded(ip, port, "/v1/models", 10000)
    if st == 200 and body and ('"data"' in body or '"models"' in body):
        import re
        ids = re.findall(r'"(?:id|name)"\s*:\s*"([^"]+)"', body)
        result["models"] = len(ids) if ids else -1
    elif st == 401:
        result["chat_401"] = True
    # /register
    st, body = http_get_bounded(ip, port, "/register", 5000)
    result["register"] = str(st) if isinstance(st, int) else st
    if st == 200 and body:
        import re
        m = re.search(r"<title>([^<]{1,80})", body)
        if m: result["panel"] = m.group(1).strip()[:80]
        if any(x in body.lower() for x in ["__nuxt","__next","react","vue","app.js","bundle.js","assets/index"]):
            result["panel"] = (result["panel"] or "") + " [SPA]"
    # / root (if no panel yet)
    if not result["panel"]:
        st, body = http_get_bounded(ip, port, "/", 5000)
        if st == 200 and body:
            import re
            m = re.search(r"<title>([^<]{1,80})", body)
            if m: result["panel"] = m.group(1).strip()[:80]
            if any(x in body.lower() for x in ["__nuxt","__next","react","vue","app.js","bundle.js"]):
                result["panel"] = (result["panel"] or "") + " [SPA]"
    return result

def _http_probe(ip, port=PORT):
    base_url = f"http://{ip}:{port}"
    result = {"ip": ip, "port": port, "models": None, "register": None, "panel": None, "chat_401": False}
    # /v1/models (no auth)
    try:
        req = urllib.request.Request(base_url + "/v1/models", headers=UA)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read(10000).decode("utf-8", "ignore")
            if '"data"' in body or '"models"' in body:
                import re
                ids = re.findall(r'"(?:id|name)"\s*:\s*"([^"]+)"', body)
                result["models"] = len(ids) if ids else -1
    except urllib.error.HTTPError as e:
        if e.code == 401: result["chat_401"] = True
        if e.code == 404: result["models"] = "404"
    except:
        pass
    # /register
    try:
        req = urllib.request.Request(base_url + "/register", headers=UA)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read(5000).decode("utf-8", "ignore")
            if resp.status == 200:
                result["register"] = "200"
                import re
                m = re.search(r"<title>([^<]{1,80})", body)
                if m: result["panel"] = m.group(1).strip()[:80]
                # check for SPA indicators
                if any(x in body.lower() for x in ["__nuxt", "__next", "react", "vue", "app.js", "bundle.js", "assets/index"]):
                    result["panel"] = (result["panel"] or "") + " [SPA]"
    except urllib.error.HTTPError as e:
        result["register"] = str(e.code)
    except:
        pass
    # also check / root
    if not result["panel"]:
        try:
            req = urllib.request.Request(base_url + "/", headers=UA)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read(5000).decode("utf-8", "ignore")
                import re
                m = re.search(r"<title>([^<]{1,80})", body)
                if m: result["panel"] = m.group(1).strip()[:80]
                if any(x in body.lower() for x in ["__nuxt", "__next", "react", "vue", "app.js", "bundle.js"]):
                    result["panel"] = (result["panel"] or "") + " [SPA]"
        except:
            pass
    return result

def main():
    with open(f"shards/{SHARD}.json") as f:
        data = json.load(f)
    ips = data["ips"]
    print(f"[{SHARD}] scanning {len(ips)} IPs on :{PORT}...", flush=True)

    # phase 1: TCP sweep
    t0 = time.time()
    alive = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = {ex.submit(tcp_check, ip): ip for ip in ips}
        for fut in as_completed(futs):
            r = fut.result()
            if r: alive.append(r)
    tcp_time = time.time() - t0
    print(f"[{SHARD}] TCP: {len(alive)}/{len(ips)} alive in {tcp_time:.0f}s", flush=True)

    # phase 2: HTTP verify — hard per-host budget so one tarpit can't stall the shard
    t1 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=50) as ex:
        futs = {ex.submit(http_probe, ip): ip for ip in alive}
        try:
            for fut in as_completed(futs, timeout=HOST_BUDGET):
                try:
                    results.append(fut.result())
                except:
                    pass
        except TimeoutError:
            # leave stragglers behind rather than stall to the runner timeout
            pass
    # cancel anything still running
    for f in futs:
        try: f.cancel()
        except: pass
    http_time = time.time() - t1
    print(f"[{SHARD}] HTTP: {len(results)}/{len(alive)} done in {http_time:.0f}s", flush=True)

    # output
    out = {
        "shard": SHARD,
        "total_scanned": len(ips),
        "alive_3000": alive,
        "verified": results,
        "tcp_time": tcp_time,
        "http_time": http_time,
    }
    with open(f"result-{SHARD}.json", "w") as f:
        json.dump(out, f, indent=1)

    models_ok = [r for r in results if isinstance(r.get("models"), int) and r["models"] > 0]
    register_ok = [r for r in results if r.get("register") == "200"]
    print(f"[{SHARD}] SUMMARY: alive={len(alive)} models_readable={len(models_ok)} register_200={len(register_ok)}", flush=True)
    for r in models_ok: print(f"  MODELS {r['ip']}:{r['port']} -> {r['models']} | {r.get('panel','')}", flush=True)
    for r in register_ok: print(f"  REG {r['ip']}:{r['port']} | {r.get('panel','')} | models={r.get('models')}", flush=True)

if __name__ == "__main__":
    main()
