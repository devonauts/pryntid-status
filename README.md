# Prynt Status · status.pryntid.com

Public status page for Prynt, measured **from outside our network** by GitHub Actions every 5 minutes and
served by GitHub Pages. Nothing here runs on our servers, so the page stays up when Prynt is down.

| File | What it is |
|---|---|
| `probe.py` | The probe (Python standard library only). Checks each component, keeps 90 days of history, writes `status.json`, sends Telegram alerts. |
| `components.json` | What is checked. `json` = fields the JSON body must contain; `marker` = text the body must contain; `critical` = its outage is a "major outage". |
| `.github/workflows/probe.yml` | Every 5 min: restore state from the `data` branch, probe, force-push `status.json`, `history.jsonl`, `alerts.json` to `data` (single commit). |
| `index.html` | The page. Reads `status.json` from `raw.githubusercontent.com/.../data/`; English/Spanish. |
| `CNAME` | Custom domain for GitHub Pages. |

**A check passes** when the answer is 2xx/3xx, arrives within 12 s, has the expected body and is not a
Cloudflare error page. A failure is re-checked after 3 s before it counts. Slower than 5 s = "slow"
(counts as up for uptime). **Down** for alerts and incidents = 2 failed runs in a row (~5–10 min).

**Alerts:** Telegram, HQ group, topic *srv* (Alertas): one message when a component goes down, one when it recovers.
Repo secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_THREAD_ID`.

**DNS:** Cloudflare zone `pryntid.com`: `CNAME status → devonauts.github.io`, DNS only (grey cloud).
Pages: branch `main`, root, custom domain `status.pryntid.com`, HTTPS enforced.

Add a component: edit `components.json`, push, run the workflow by hand (Actions → probe → Run workflow).
