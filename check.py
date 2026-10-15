#!/usr/bin/env python3
"""Procket's status page: check each service, keep 90 days of history, and
render the page. It is a dashboard only: nothing is sent when a check fails.

Runs every 5 minutes from GitHub Actions in the public procket-dev/status
repository, and publishes to GitHub Pages at status.procket.dev. Nothing here
runs on Procket's own AWS account, so an outage there cannot take the status
page down with it.

Standard library only:

    python3 check.py --config config.json --out site

Settings from the environment (all optional, as in tests):
    GITHUB_TOKEN, GITHUB_REPOSITORY  read the incidents posted as issues
    GITHUB_API_URL                   default https://api.github.com
    GITHUB_OUTPUT                    where the workflow reads `publish` and `keepalive`
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HISTORY_DAYS = 90
LABEL = "incident"
# Incidents written by these are shown. Anyone can open an issue in a public
# repository; only people with access to it post incidents.
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
INCIDENT_DAYS = 30


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


# --- checking ---------------------------------------------------------------------


def probe(check, timeout=10.0, retries=1, pause=5.0):
    """One check: a GET that must answer with the expected status in time.
    A failure is retried once, so one dropped connection is not an outage."""
    expect = check.get("expect", 200)
    result = {}
    for attempt in range(retries + 1):
        start = time.monotonic()
        code, error = 0, ""
        try:
            req = urllib.request.Request(check["url"], headers={"User-Agent": "procket-status"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                resp.read(65536)
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # timeouts, DNS, TLS, refused connections
            error = str(getattr(e, "reason", e)) or e.__class__.__name__
        ms = int((time.monotonic() - start) * 1000)
        ok = code == expect
        if not ok and not error:
            error = f"answered {code}, expected {expect}"
        result = {"ok": ok, "code": code, "ms": ms, "error": "" if ok else error}
        if ok:
            break
        if attempt < retries:
            time.sleep(pause)
    return result


def update(state, checks, results, now):
    """Fold one run's results into the state. Returns the checks whose status
    changed: [(id, entry)]."""
    today = now.strftime("%Y-%m-%d")
    oldest = (now - dt.timedelta(days=HISTORY_DAYS - 1)).strftime("%Y-%m-%d")
    entries = state.setdefault("checks", {})
    changed = []
    for check in checks:
        cid, r = check["id"], results[check["id"]]
        e = entries.setdefault(cid, {"ok": True, "since": iso(now), "failures": 0, "days": {}})
        day = e["days"].setdefault(today, [0, 0])
        day[0] += 1 if r["ok"] else 0
        day[1] += 1
        e["days"] = {d: v for d, v in e["days"].items() if d >= oldest}
        e["failures"] = 0 if r["ok"] else e.get("failures", 0) + 1
        if r["ok"] != e.get("ok", True):
            e["since"] = iso(now)
            changed.append((cid, e))
        e.update(ok=r["ok"], code=r["code"], ms=r["ms"], error=r["error"], name=check["name"])
    # Checks removed from the config are forgotten.
    for cid in list(entries):
        if cid not in {c["id"] for c in checks}:
            del entries[cid]
    state["updated"] = iso(now)
    return changed


def uptime(days):
    up = sum(v[0] for v in days.values())
    total = sum(v[1] for v in days.values())
    return None if total == 0 else 100.0 * up / total


# --- GitHub ---------------------------------------------------------------------------


class GitHub:
    def __init__(self, token, repo, api="https://api.github.com"):
        self.token, self.repo, self.api = token, repo, api.rstrip("/")

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "procket-status",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def incidents(self, now):
        since = iso(now - dt.timedelta(days=INCIDENT_DAYS))
        query = urllib.parse.urlencode({"labels": LABEL, "state": "all", "since": since, "per_page": 20,
                                        "sort": "created", "direction": "desc"})
        issues = self.call("GET", f"/repos/{self.repo}/issues?{query}") or []
        out = []
        for i in issues:
            if "pull_request" in i or not trusted(i):
                continue
            # Only incidents still open, or resolved within the window.
            if i["state"] == "closed" and (i.get("closed_at") or "") < since:
                continue
            updates = [{"at": i["created_at"], "text": i.get("body") or ""}]
            if i.get("comments"):
                for c in self.call("GET", f"/repos/{self.repo}/issues/{i['number']}/comments?per_page=50") or []:
                    if trusted(c):
                        updates.append({"at": c["created_at"], "text": c.get("body") or ""})
            out.append({"number": i["number"], "title": i["title"], "open": i["state"] == "open",
                        "created": i["created_at"], "closed": i.get("closed_at"), "url": i["html_url"],
                        "updates": updates})
        return out


def trusted(item):
    return item.get("author_association") in TRUSTED


# --- the state, kept on the published site itself --------------------------------------


class StateUnavailable(Exception):
    pass


def load_state(source):
    """The previous run's data.json, from the live site (or a file in tests).
    A missing one starts afresh. One that cannot be read right now stops the
    run, so a network blip never wipes 90 days of history."""
    if not source:
        return {}
    if not source.startswith(("http://", "https://")):
        try:
            with open(source, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    try:
        req = urllib.request.Request(source, headers={"User-Agent": "procket-status", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise StateUnavailable(f"{source}: HTTP {e.code}") from e
    except Exception as e:
        raise StateUnavailable(f"{source}: {e}") from e


# --- rendering ------------------------------------------------------------------------


def esc(s):
    return html.escape(str(s), quote=True)


def text_to_html(text):
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join("<p>" + esc(p).replace("\n", "<br>") + "</p>" for p in paras)


def when(s):
    return parse_iso(s).strftime("%d %b %Y, %H:%M UTC")


def bars(days, now):
    cells = []
    for n in range(HISTORY_DAYS - 1, -1, -1):
        d = (now - dt.timedelta(days=n)).strftime("%Y-%m-%d")
        v = days.get(d)
        if not v or not v[1]:
            cls, label = "none", "no data"
        else:
            pct = 100.0 * v[0] / v[1]
            cls = "up" if pct == 100 else "partial" if pct >= 99 else "down"
            label = f"{pct:.2f}% up"
        cells.append(f'<span class="bar {cls}" title="{d}: {label}"></span>')
    return "".join(cells)


def render(config, state, incidents, now):
    checks = config["checks"]
    entries = state.get("checks", {})
    open_incidents = [i for i in incidents if i["open"]]
    down = [c for c in checks if not entries.get(c["id"], {}).get("ok", True)]
    if down or open_incidents:
        summary_cls = "bad" if down else "warn"
        summary = "Some systems are down" if down else "We are looking into a problem"
    else:
        summary_cls, summary = "good", "All systems operational"

    rows = []
    for c in checks:
        e = entries.get(c["id"], {})
        ok = e.get("ok", True)
        pct = uptime(e.get("days", {}))
        rows.append(f'''
      <section class="check">
        <div class="check-head">
          <div><h2>{esc(c["name"])}</h2><p class="muted">{esc(c.get("description", ""))}</p></div>
          <span class="pill {'up' if ok else 'down'}">{'Operational' if ok else 'Down'}</span>
        </div>
        <div class="bars" role="img" aria-label="{esc(c["name"])}: daily uptime for the last {HISTORY_DAYS} days">{bars(e.get("days", {}), now)}</div>
        <div class="axis muted"><span>{HISTORY_DAYS} days ago</span><span>{'' if pct is None else f'{pct:.2f}% uptime'}</span><span>Today</span></div>
      </section>''')

    def incident_html(i):
        state_txt = "Ongoing" if i["open"] else f"Resolved {when(i['closed'])}" if i.get("closed") else "Resolved"
        updates = "".join(f'<li><time>{when(u["at"])}</time>{text_to_html(u["text"])}</li>'
                          for u in reversed(i["updates"]))
        return (f'<article class="incident {"open" if i["open"] else ""}"><h3>{esc(i["title"])}</h3>'
                f'<p class="muted">{state_txt} · started {when(i["created"])}</p><ol class="updates">{updates}</ol></article>')

    incident_section = ("".join(incident_html(i) for i in incidents)
                        or f'<p class="muted">No incidents in the last {INCIDENT_DAYS} days.</p>')
    title = esc(config.get("title", "Status"))
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="Whether Procket is working, and what we are doing about it when it is not.">
<link rel="alternate" type="application/atom+xml" title="{title} incidents" href="feed.xml">
<meta http-equiv="refresh" content="300">
<style>
:root {{ --bg:#fafafa; --fg:#18181b; --muted:#71717a; --card:#fff; --line:#e4e4e7; --up:#16a34a; --partial:#f59e0b;
  --down:#dc2626; --none:#e4e4e7; --brand:#c2410c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#09090b; --fg:#f4f4f5; --muted:#a1a1aa; --card:#18181b;
  --line:#27272a; --none:#3f3f46; --brand:#f97316; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:32px 16px 64px; }}
header {{ display:flex; justify-content:space-between; align-items:baseline; gap:16px; flex-wrap:wrap; }}
header a {{ color:var(--brand); font-weight:700; font-size:1.25rem; text-decoration:none; }}
h1 {{ font-size:1rem; font-weight:500; margin:0; color:var(--muted); }}
h2 {{ font-size:1.05rem; margin:0; }} h3 {{ margin:0 0 4px; font-size:1.05rem; }}
.muted {{ color:var(--muted); font-size:.9rem; margin:0; }}
.summary {{ margin:24px 0; padding:16px 20px; border-radius:10px; color:#fff; font-weight:600; font-size:1.1rem; }}
.summary.good {{ background:var(--up); }} .summary.warn {{ background:var(--partial); color:#18181b; }} .summary.bad {{ background:var(--down); }}
.check, .incident {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px 20px; margin:12px 0; }}
.incident.open {{ border-color:var(--partial); }}
.check-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.pill {{ font-size:.85rem; font-weight:600; padding:2px 10px; border-radius:999px; white-space:nowrap; }}
.pill.up {{ color:var(--up); border:1px solid var(--up); }} .pill.down {{ color:#fff; background:var(--down); }}
.bars {{ display:flex; gap:2px; margin:14px 0 6px; height:32px; }}
.bar {{ flex:1; border-radius:2px; min-width:2px; }} .bar.up {{ background:var(--up); }} .bar.partial {{ background:var(--partial); }}
.bar.down {{ background:var(--down); }} .bar.none {{ background:var(--none); }}
.axis {{ display:flex; justify-content:space-between; font-size:.8rem; }}
section.incidents h2 {{ margin:40px 0 4px; }}
.updates {{ list-style:none; padding:0; margin:12px 0 0; }} .updates li {{ border-left:2px solid var(--line); padding:0 0 8px 12px; }}
.updates time {{ font-size:.85rem; color:var(--muted); }} .updates p {{ margin:4px 0; }}
footer {{ margin-top:40px; }} footer a {{ color:inherit; }}
@media (max-width:480px) {{ .bars {{ gap:1px; }} }}
</style>
</head>
<body>
<main>
  <header><a href="{esc(config.get("home", "https://procket.dev"))}">Procket</a><h1>Status</h1></header>
  <div class="summary {summary_cls}">{summary}</div>
  {"".join(rows)}
  <section class="incidents">
    <h2>Incidents</h2>
    {incident_section}
  </section>
  <footer class="muted">
    <p>Checked every 5 minutes from GitHub's servers, independently of Procket's own. Last checked
      {when(state["updated"]) if state.get("updated") else "never"}.
      Pull request comments also depend on <a href="https://www.githubstatus.com">GitHub</a>.
      Follow incidents with the <a href="feed.xml">Atom feed</a>.</p>
  </footer>
</main>
</body>
</html>
'''


def feed(config, incidents, now):
    base = config.get("site_url", "").rstrip("/")
    entries = []
    for i in incidents:
        latest = i["updates"][-1]
        status = "Ongoing" if i["open"] else "Resolved"
        entries.append(f'''  <entry>
    <id>{esc(i["url"])}</id>
    <title>{esc(status + ": " + i["title"])}</title>
    <updated>{esc(i.get("closed") if not i["open"] and i.get("closed") else latest["at"])}</updated>
    <link href="{esc(base + "/")}"/>
    <content type="text">{esc(latest["text"])}</content>
  </entry>''')
    return f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{esc(base + "/feed.xml")}</id>
  <title>{esc(config.get("title", "Status"))}</title>
  <updated>{iso(now)}</updated>
  <link href="{esc(base + "/")}"/>
{chr(10).join(entries)}
</feed>
'''


# --- the run ---------------------------------------------------------------------------


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def run(config, state_source, out_dir, gh=None, now=None, probe_fn=probe):
    now = now or utcnow()
    state = load_state(state_source)
    checks = config["checks"]
    results = {c["id"]: probe_fn(c) for c in checks}
    update(state, checks, results, now)
    incidents = []
    if gh:
        incidents = gh.incidents(now)
    # Scheduled workflows in a public repository stop after 60 days without
    # activity, so the workflow commits a line about once a month.
    keepalive = state.get("keepalive")
    if not keepalive or now - parse_iso(keepalive) > dt.timedelta(days=25):
        state["keepalive"] = iso(now)
        output("keepalive", "true")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render(config, state, incidents, now))
    with open(os.path.join(out_dir, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed(config, incidents, now))
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    if config.get("domain"):
        with open(os.path.join(out_dir, "CNAME"), "w", encoding="utf-8") as f:
            f.write(config["domain"] + "\n")
    return state, incidents


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--state", help="previous data.json: a URL or a file (default: the live site's)")
    ap.add_argument("--out", default="site")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    source = args.state if args.state is not None else config.get("site_url", "").rstrip("/") + "/data.json"
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    gh = GitHub(token, repo, os.environ.get("GITHUB_API_URL", "https://api.github.com")) if token and repo else None
    try:
        state, _ = run(config, source, args.out, gh)
    except StateUnavailable as e:
        # Publishing now would replace 90 days of history with one run.
        print(f"previous state unavailable, not publishing: {e}", file=sys.stderr)
        output("publish", "false")
        return 0
    output("publish", "true")
    for cid, e in state["checks"].items():
        print(f"{cid}: {'up' if e['ok'] else 'DOWN'} ({e['code']}, {e['ms']} ms){' ' + e['error'] if e['error'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
