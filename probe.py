#!/usr/bin/env python3
"""status.pryntid.com — external probe for Prynt, run by GitHub Actions every 5 minutes.

Each run:
  1. checks every component in components.json from GitHub's network (outside our servers);
  2. appends the run to <state>/history.jsonl (90 days kept);
  3. writes <state>/status.json (current state, uptime 24h/7d/30d/90d, 90 daily bars, incidents)
     that index.html reads from the `data` branch;
  4. sends Telegram alerts on confirmed down / recovered (2 failed runs in a row = down).

Standard library only. Usage: python3 -I probe.py --conf components.json --state data
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "pryntid-status-probe/1 (+https://status.pryntid.com)"
HISTORY_DAYS = 90
INCIDENT_DAYS = 30
CONFIRM_RUNS = 2          # consecutive failed runs before a component counts as "down" for alerts
RETRY_DELAY = 3           # seconds before re-checking a failed probe inside the same run
EDGE_ERROR_MARKS = ("cf-error-details", "Error 52", "error code: 52", "Bad gateway", "Origin is unreachable")


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Cache-Control": "no-cache"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            body = r.read(512 * 1024).decode("utf-8", "replace")
            return r.status, body, int((time.monotonic() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        body = e.read(64 * 1024).decode("utf-8", "replace") if e.fp else ""
        return e.code, body, int((time.monotonic() - t0) * 1000), None
    except Exception as e:  # timeouts, DNS, TLS, resets
        return None, "", int((time.monotonic() - t0) * 1000), f"{type(e).__name__}: {e}"[:160]


def check_once(c, conf):
    timeout = conf.get("timeout_seconds", 12)
    code, body, ms, err = fetch(c["url"], timeout)
    if err:
        return "down", ms, err
    accept = c.get("accept_codes")
    if (code not in accept) if accept else not (200 <= code < 400):
        return "down", ms, f"HTTP {code}"
    if any(m in body for m in EDGE_ERROR_MARKS):
        return "down", ms, f"HTTP {code} edge error page"
    if "json" in c:
        try:
            j = json.loads(body)
        except ValueError:
            return "down", ms, f"HTTP {code} body is not JSON"
        for k, v in c["json"].items():
            if j.get(k) != v:
                return "down", ms, f"HTTP {code} {k}={j.get(k)!r} (want {v!r})"
    if c.get("marker") and c["marker"] not in body:
        return "down", ms, f"HTTP {code} marker missing"
    if ms > conf.get("slow_seconds", 5) * 1000:
        return "slow", ms, f"HTTP {code} in {ms} ms"
    return "up", ms, f"HTTP {code} in {ms} ms"


def probe(c, conf):
    status, ms, detail = check_once(c, conf)
    if status == "down":  # confirm before counting it
        time.sleep(RETRY_DELAY)
        status, ms, detail = check_once(c, conf)
    return {"id": c["id"], "status": status, "ms": ms, "detail": detail}


def load_history(path, now):
    cutoff = now - timedelta(days=HISTORY_DAYS)
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = parse_iso(r.get("t"))
                if t and t >= cutoff:
                    r["_t"] = t
                    rows.append(r)
    except OSError:
        pass
    return rows


def save_history(path, rows):
    write_atomic(path, "".join(json.dumps({k: v for k, v in r.items() if k != "_t"}, separators=(",", ":")) + "\n" for r in rows))


def pct(up, total):
    return None if not total else round(100.0 * up / total, 3)


def summarize(rows, cid, now):
    windows = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30), "90d": timedelta(days=90)}
    uptime = {}
    for name, span in windows.items():
        sel = [r["s"][cid] for r in rows if cid in r["s"] and r["_t"] >= now - span]
        uptime[name] = pct(sum(1 for s in sel if s != "down"), len(sel))
    days = {}
    for r in rows:
        if cid in r["s"]:
            d = days.setdefault(r["_t"].date().isoformat(), [0, 0])
            d[1] += 1
            d[0] += r["s"][cid] != "down"
    bars = []
    for n in range(HISTORY_DAYS - 1, -1, -1):
        day = (now - timedelta(days=n)).date().isoformat()
        up, total = days.get(day, (0, 0))
        bars.append({"d": day, "u": pct(up, total), "n": total})
    return uptime, bars


def incidents(rows, components, now):
    """Automatic incidents: runs of >= CONFIRM_RUNS consecutive 'down' results per component."""
    since = now - timedelta(days=INCIDENT_DAYS)
    out = []
    for c in components:
        cid, streak = c["id"], []
        seq = [r for r in rows if cid in r["s"]]
        for i, r in enumerate(seq + [None]):
            if r is not None and r["s"][cid] == "down":
                streak.append(r)
                continue
            if len(streak) >= CONFIRM_RUNS:
                start, last = streak[0]["_t"], streak[-1]["_t"]
                ongoing = r is None
                end = None if ongoing else r["_t"]
                if (end or now) >= since:
                    out.append({"component": cid, "start": iso(start), "end": iso(end) if end else None,
                                "ongoing": ongoing, "minutes": int(((end or now) - start).total_seconds() // 60) or 5,
                                "detail": (streak[-1].get("d") or {}).get(cid, "")})
            streak = []
    out.sort(key=lambda i: i["start"], reverse=True)
    return out


def overall(results, components):
    crit = {c["id"] for c in components if c.get("critical")}
    down = [r for r in results if r["status"] == "down"]
    if any(r["id"] in crit for r in down):
        return "major"
    if down:
        return "partial"
    if any(r["status"] == "slow" for r in results):
        return "degraded"
    return "ok"


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram: not configured, skipping:", text)
        return
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if os.environ.get("TELEGRAM_THREAD_ID"):
        data["message_thread_id"] = os.environ["TELEGRAM_THREAD_ID"]
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urllib.parse.urlencode(data).encode(), headers={"User-Agent": UA}), timeout=15).read()
    except Exception as e:
        print("telegram: send failed:", type(e).__name__, file=sys.stderr)


def notify(alerts, results, components, public_url):
    names = {c["id"]: c["name"]["es"] for c in components}
    for r in results:
        a = alerts.setdefault(r["id"], {"fails": 0, "alerted": False})
        if r["status"] == "down":
            a["fails"] += 1
            if a["fails"] >= CONFIRM_RUNS and not a["alerted"]:
                a["alerted"], a["since"] = True, iso(now_utc())
                telegram(f"🔴 Prynt · {names[r['id']]} CAÍDO (desde fuera, {a['fails']} sondas seguidas)\n{r['detail']}\n{public_url}")
        else:
            if a["alerted"]:
                telegram(f"🟢 Prynt · {names[r['id']]} recuperado ({r['detail']})\n{public_url}")
            alerts[r["id"]] = {"fails": 0, "alerted": False}
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default="components.json")
    ap.add_argument("--state", default="data")
    ap.add_argument("--no-alerts", action="store_true")
    a = ap.parse_args()

    conf = read_json(a.conf, None)
    if not conf:
        sys.exit(f"cannot read {a.conf}")
    components = conf["components"]
    os.makedirs(a.state, exist_ok=True)
    hist_path = os.path.join(a.state, "history.jsonl")

    now = now_utc()
    results = [probe(c, conf) for c in components]
    for r in results:
        print(f"{r['id']:8} {r['status']:5} {r['detail']}")

    rows = load_history(hist_path, now)
    row = {"t": iso(now), "s": {r["id"]: r["status"] for r in results}, "ms": {r["id"]: r["ms"] for r in results}}
    down_detail = {r["id"]: r["detail"] for r in results if r["status"] != "up"}
    if down_detail:
        row["d"] = down_detail
    row["_t"] = now
    rows.append(row)
    save_history(hist_path, rows)

    by_id = {r["id"]: r for r in results}
    comps = []
    for c in components:
        uptime, bars = summarize(rows, c["id"], now)
        r = by_id[c["id"]]
        comps.append({"id": c["id"], "name": c["name"], "description": c.get("description", {}), "host": urllib.parse.urlsplit(c["url"]).netloc,
                      "critical": bool(c.get("critical")), "status": r["status"], "ms": r["ms"], "detail": r["detail"],
                      "uptime": uptime, "days": bars})
    status = {"generatedAt": iso(now), "intervalMinutes": 5, "overall": overall(results, components),
              "components": comps, "incidents": incidents(rows, components, now), "runs": len(rows)}
    write_atomic(os.path.join(a.state, "status.json"), json.dumps(status, separators=(",", ":")))

    alerts_path = os.path.join(a.state, "alerts.json")
    alerts = read_json(alerts_path, {})
    if not a.no_alerts:
        alerts = notify(alerts, results, components, conf.get("public_url", "https://status.pryntid.com"))
    write_atomic(alerts_path, json.dumps(alerts, indent=1))
    print("overall:", status["overall"])


if __name__ == "__main__":
    main()
