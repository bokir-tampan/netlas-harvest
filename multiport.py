#!/usr/bin/env python3
"""Multi-port sweep of known-alive :3000 hosts for native vLLM/Ollama/xinference services.
Ports: 8000 (vLLM native), 8080 (generic), 11434 (Ollama native), 8001 (xinference).
Reads ips.json (list of IPs), outputs result.json with per-port findings.
"""
import json, os, socket, urllib.request, urllib.error, concurrent.futures, sys

PORTS = [8000, 8080, 11434, 8001, 5000]
TIMEOUT = 5
HTTP_TIMEOUT = 6
UA = {"User-Agent": "Mozilla/5.0"}

IPS = os.environ.get("IPS", "ips.json")
OUT = os.environ.get("OUT", "result.json")

def tcp_open(ip, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False

def http_get(ip, port, path):
    req = urllib.request.Request(f"http://{ip}:{port}{path}", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read(8000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(3000).decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)[:60]

def probe_ip(ip):
    found = []
    for port in PORTS:
        if not tcp_open(ip, port):
            continue
        svc = None
        models = []
        # Ollama native: /api/tags lists models
        st, body = http_get(ip, port, "/api/tags")
        if st == 200 and '"models"' in body:
            try:
                j = json.loads(body)
                models = [m.get("name") for m in j.get("models", [])]
                svc = "ollama"
            except Exception:
                svc = "ollama?"
        # vLLM / openai-compatible: /v1/models
        if not svc:
            st, body = http_get(ip, port, "/v1/models")
            if st == 200 and ('"data"' in body or '"models"' in body):
                try:
                    j = json.loads(body)
                    models = [m.get("id") for m in j.get("data", j.get("models", []))]
                    svc = "openai-compat"
                except Exception:
                    svc = "openai-compat?"
            elif st == 401:
                svc = "openai-auth"
        # xinference: /v1/models too, but root / has xinference
        if not svc:
            st, body = http_get(ip, port, "/")
            if st == 200:
                low = body.lower()
                if "xinference" in low:
                    svc = "xinference"
                elif "vllm" in low:
                    svc = "vllm-root"
                elif "ollama" in low:
                    svc = "ollama-root"
        found.append({"port": port, "svc": svc, "models": len(models),
                      "model_samples": models[:8] if models else []})
    return ip, found

def main():
    ips = json.load(open(IPS))
    print(f"probing {len(ips)} IPs x {len(PORTS)} ports", flush=True)
    results = []
    live = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        for ip, found in ex.map(probe_ip, ips):
            if found:
                results.append({"ip": ip, "ports": found})
                live.append(ip)
                for f in found:
                    tag = f["svc"] or "?"
                    mtag = f" ({f['models']} models)" if f["models"] else ""
                    print(f"  HIT {ip}:{f['port']} {tag}{mtag}", flush=True)
    out = {"total_ips": len(ips), "hits": len(results), "results": results}
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"\nDONE: {len(results)} IPs with open non-3000 service ports", flush=True)

if __name__ == "__main__":
    main()
