# Procket status page

`status.procket.dev`: whether Procket is working, and what we are doing about
it when it is not.

It runs entirely on GitHub, in a public repository of its own
(`procket-dev/status`), and not on Procket's AWS account. If that account or
its region has a problem, the status page stays up to say so. It costs nothing:
Actions minutes and Pages are free for public repositories.

- **Checks:** every 5 minutes, [`check.py`](check.py) requests each URL in
  [`config.json`](config.json) and expects a 200. A failure is retried once
  before it counts. GitHub sometimes starts scheduled runs a few minutes late.
- **History:** 90 days per check, shown as one bar per day. It is kept in the
  published site's own `data.json` and read back by the next run, so nothing
  is committed every 5 minutes. If the previous `data.json` cannot be read, the
  run publishes nothing rather than starting the history again.
- **Outages:** a failing check shows on the page as down, and in its history.
  Nothing is sent: the page is a dashboard, not an alarm. To tell customers
  more, post an incident.
- **Incidents:** they are issues labelled `incident`. Open one to post an
  incident; comment on it to post an update; close it when it is resolved. The
  page updates within a minute or two. Only issues and comments from people with
  access to the repository are shown, because anyone can open an issue in a
  public repository. Incidents from the last 30 days are listed, and there is an
  Atom feed at `/feed.xml`.

## Setting it up

1. Create the public repository `procket-dev/status` and push the contents of
   this directory to it, with `.github/` at its root.
2. Create the label `incident` (Issues → Labels).
3. Settings → Pages → Source: **GitHub Actions**. Then set the custom domain to
   `status.procket.dev` and tick "Enforce HTTPS" once the certificate is ready.
4. DNS: a `CNAME` record from `status.procket.dev` to `procket-dev.github.io`.
   Put it with whichever provider holds `procket.dev`. It should not depend on
   Procket's AWS account if you can help it.
5. Run the **Status** workflow once by hand with **Start fresh** ticked. Every
   other run reads the history back from the live site and publishes nothing
   if it cannot, so the first run, with no site yet, needs it. Do not tick it
   again: it replaces the history with one run.

Until the custom domain serves HTTPS, scheduled runs cannot read the history
and publish nothing; the page from the first run stays up meanwhile. Until a
service exists, take its check out of `config.json`: a missing service would
show as down.

To change the page later, edit it here and copy the files over a clone of
`procket-dev/status`, rather than pushing this directory's history: the
workflow commits `.keepalive` to that repository about once a month.

## Posting an incident

Write for customers, not for us. Say what they see, what is affected and what
is not, and when the next update will come.

> **Title:** Pull request comments delayed
>
> GitHub is rate limiting us, so new pull request comments are queued. They
> will appear once it eases. Recording prompts is not affected. Next update by
> 12:30 UTC.

For planned maintenance, open the issue 48 hours ahead with the time in the
title, and close it when the work is done.

## Testing

```sh
python3 -m unittest discover -s .
python3 check.py --state /tmp/data.json --out /tmp/site   # a real run, without GitHub
```
