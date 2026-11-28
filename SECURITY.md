# Security policy

hullgate sits in front of MCP servers and holds their credentials, so a vulnerability in it can
expose the services behind it. Thank you for reporting one privately.

## Supported versions

Only the latest release receives security fixes. Older releases are not patched: update to the
latest release to get a fix.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open
[a private report](https://github.com/sstolz/hullgate/security/advisories/new) from the
repository's **Security** tab. It is the one channel for vulnerabilities; it keeps the report
visible to the maintainer only until an advisory is published.

Please do not open a public issue, pull request or discussion for a vulnerability.

A useful report contains:

- the affected version (release tag or commit);
- the component concerned (public listener, authorization server, relay, administration
  listener or console, command line, deployment files);
- the steps to reproduce, with the configuration needed, and what you observed;
- the impact you expect (what an attacker gains, under which preconditions);
- any proof of concept or suggested fix.

Leave real secrets out of the report: replace tokens, keys and passwords by placeholders.

## What to expect

- An acknowledgement of the report, in the private report itself.
- An assessment: whether the report is confirmed, its severity, and the releases affected.
- A fix in a new release when the report is confirmed, prepared in a private fork of the advisory.
- A published GitHub security advisory describing the issue and the fixed release, crediting you
  unless you ask otherwise.
