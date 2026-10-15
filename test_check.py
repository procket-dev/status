"""Tests for check.py: python3 -m unittest discover -s status"""

import datetime as dt
import http.server
import json
import os
import tempfile
import threading
import unittest

import check

NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)


class Server:
    """A local HTTP server: /up answers 200, /down 503, and a tiny fake of the
    GitHub issues API under /repos/."""

    def __init__(self):
        self.issues, self.comments, self.calls = {}, {}, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=None):
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self):
                outer.calls.append(("GET", self.path))
                if self.path == "/up":
                    return self.reply(200, {})
                if self.path == "/down":
                    return self.reply(503, {})
                if "/issues?" in self.path:
                    return self.reply(200, list(outer.issues.values()))
                if self.path.endswith("/comments?per_page=50"):
                    n = int(self.path.split("/")[-2])
                    return self.reply(200, outer.comments.get(n, []))
                self.reply(404)

            def do_POST(self):
                outer.calls.append(("POST", self.path))
                body = self.body()
                if self.path.endswith("/issues"):
                    n = len(outer.issues) + 1
                    outer.issues[n] = {"number": n, "title": body["title"], "body": body["body"], "state": "open",
                                       "created_at": check.iso(NOW), "closed_at": None, "comments": 0,
                                       "html_url": f"https://github.com/x/status/issues/{n}",
                                       "user": {"login": check.BOT}, "author_association": "NONE",
                                       "labels": body["labels"]}
                    return self.reply(201, outer.issues[n])
                if self.path.endswith("/comments"):
                    n = int(self.path.split("/")[-2])
                    outer.comments.setdefault(n, []).append({"body": body["body"], "created_at": check.iso(NOW),
                                                             "author_association": "NONE",
                                                             "user": {"login": check.BOT}})
                    outer.issues[n]["comments"] += 1
                    return self.reply(201, {})
                self.reply(404)

            def do_PATCH(self):
                outer.calls.append(("PATCH", self.path))
                n = int(self.path.split("/")[-1])
                outer.issues[n].update(self.body(), closed_at=check.iso(NOW))
                self.reply(200, outer.issues[n])

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def fast_probe(c):
    return check.probe(c, timeout=5, retries=0)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.srv = Server()
        self.addCleanup(self.srv.close)
        self.dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.dir, "site", "data.json")
        self.out = os.path.join(self.dir, "site")

    def config(self, service="/up"):
        return {"title": "Procket status", "site_url": "https://status.example", "domain": "status.example",
                "checks": [{"id": "service", "name": "Service", "url": self.srv.url + service},
                           {"id": "website", "name": "Website", "url": self.srv.url + "/up"}]}

    def run_once(self, service="/up", now=NOW, gh=None):
        return check.run(self.config(service), self.state_file, self.out, gh=gh, now=now, probe_fn=fast_probe)

    def page(self):
        with open(os.path.join(self.out, "index.html"), encoding="utf-8") as f:
            return f.read()

    def test_history_survives_runs_and_is_rendered(self):
        self.run_once()
        state, _ = self.run_once("/down", now=NOW + dt.timedelta(minutes=5))
        e = state["checks"]["service"]
        self.assertFalse(e["ok"])
        self.assertEqual(e["days"]["2026-10-11"], [1, 2])
        self.assertIn("503", e["error"])
        page = self.page()
        self.assertIn("Some systems are down", page)
        self.assertIn("50.00% up", page)
        with open(os.path.join(self.out, "CNAME"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "status.example")

        self.run_once(now=NOW + dt.timedelta(minutes=10))
        self.assertIn("All systems operational", self.page())

    def test_history_keeps_90_days(self):
        self.run_once()
        state, _ = self.run_once(now=NOW + dt.timedelta(days=95))
        days = state["checks"]["service"]["days"]
        self.assertEqual(list(days), ["2027-01-14"])

    def test_an_outage_shows_on_the_page_and_sends_nothing(self):
        # Dashboard only: a failing check is shown, and no issue is opened, so
        # nobody is emailed.
        gh = check.GitHub("token", "x/status", self.srv.url)
        for minutes in (0, 5, 10):
            state, _ = self.run_once("/down", now=NOW + dt.timedelta(minutes=minutes), gh=gh)
        self.assertFalse(state["checks"]["service"]["ok"])
        self.assertEqual(self.srv.issues, {})
        self.assertFalse([c for c in self.srv.calls if c[0] != "GET"], "the run wrote to GitHub")

    def test_only_trusted_people_post_incidents(self):
        gh = check.GitHub("token", "x/status", self.srv.url)
        base = {"state": "open", "created_at": check.iso(NOW), "closed_at": None, "html_url": "u", "comments": 0}
        self.srv.issues[1] = dict(base, number=1, title="Slow reports", body="We are <b>on it</b>",
                                  author_association="OWNER", user={"login": "owner"})
        self.srv.issues[2] = dict(base, number=2, title="FAKE OUTAGE", body="x",
                                  author_association="NONE", user={"login": "stranger"})
        self.run_once(gh=gh)
        page = self.page()
        self.assertIn("Slow reports", page)
        self.assertIn("We are &lt;b&gt;on it&lt;/b&gt;", page)
        self.assertNotIn("FAKE OUTAGE", page)
        self.assertIn("We are looking into a problem", page)
        with open(os.path.join(self.out, "feed.xml"), encoding="utf-8") as f:
            self.assertIn("Ongoing: Slow reports", f.read())

    def test_unreadable_state_stops_the_run(self):
        with self.assertRaises(check.StateUnavailable):
            check.load_state(self.srv.url + "/down")
        self.assertEqual(check.load_state(self.srv.url + "/missing"), {})

    def test_keepalive_about_monthly(self):
        out = os.path.join(self.dir, "github_output")
        os.environ["GITHUB_OUTPUT"] = out
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT")
        self.run_once()
        self.run_once(now=NOW + dt.timedelta(days=10))
        self.run_once(now=NOW + dt.timedelta(days=26))
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read().count("keepalive=true"), 2)


if __name__ == "__main__":
    unittest.main()
