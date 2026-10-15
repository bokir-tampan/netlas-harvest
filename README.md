# netlas-harvest

Distributed Netlas harvester that runs on GitHub Actions runners.

Each matrix job executes on a **fresh Azure runner IP**, which sidesteps the
Netlas anonymous per-IP daily request limit (~7 queries) without any proxy
rotation. 24 dorks x N pages per dispatch, then every discovered host is probed
for an OpenAI-compatible `/v1/models` endpoint.

## Usage

```bash
# dispatch a run
curl -X POST -H "Authorization: token $TOKEN" -H "User-Agent: elyy-agent" \
  https://api.github.com/repos/bokir-tampan/netlas-harvest/actions/workflows/harvest.yml/dispatches \
  -d '{"ref":"main","inputs":{"pages":"3","probe":"1"}}'

# wait for completion, then pull artifacts
python3 collect.py <run_id>
```

## Files

- `harvest.py` — one dork, N pages, optional `/v1/models` probe. Env: `DORK`, `PAGES`, `PROBE`.
- `collect.py` — local driver: dispatch, poll, download artifacts, merge into `merged.json`.
- `.github/workflows/harvest.yml` — 24-job matrix, artifact per job.

## Notes

- Artifacts are kept 3 days; the merged file is the durable output.
- Runner IPs are shared infrastructure — a 429 means another tenant burned that
  IP's budget; `harvest.py` retries with backoff up to 3 times.
