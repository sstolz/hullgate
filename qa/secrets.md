# Offline secret checks (F-022)

`make qa-secrets` runs the bundled hullgate secret scanner 1.1.0. `make qa` treats
its `FINDINGS` and `ERROR` verdicts as blocking. It is deliberately outside `make gate`.
The scanner makes no network calls, validates no credential, and emits no candidate
bytes. Reports contain path, line, detector, SHA-256/96 fingerprint and `[REDACTED]`.
Git/scanner errors never echo subprocess output or candidate values.

`qa/secrets-scanner.json` pins the scanner version, its complete source SHA-256 and
the Python 3.13.18 execution image by digest (the same base as the product Dockerfile).
The ordinary target uses native Python so it works without Docker. This is a bundled
scanner plus a pinned runtime image, not a downloaded third-party scanner image.
Changing detectors requires reviewing the source, version and checksum together;
the pin is checked at runtime. `QA_IMAGE_secrets` declares that runtime in Makefile.

The scanner reads every tracked working-tree file outside the workshop and local
context, including binary bytes and the public context example. It reads symlink
text, never its target; an unexpected symlink, submodule, unreadable file or tracked
credential path fails closed. There is no size, extension, entropy or test-directory
exemption. Untracked files are outside the population; stage additions before scanning.

The detectors cover private PEM and PGP key blocks, GitHub/GitLab/AWS/Google/OpenAI/Slack/
Stripe token formats, hullgate tokens, JWTs, bearer strings, URL passwords and quoted or
unquoted secret-like assignments. They also cover `Authorization: token` with 40 hex
characters, `Authorization: Basic` base64 values (at least 8 characters), assignments
ending in `_key` or `-key` (including signing keys and base64 values, at least 16
characters), labelled TOTP seeds (base32, at least 16 characters), and assignments
ending in `password`, `passwd` or `pwd` (at least 4 characters, including punctuation).
Quoted passwords can contain spaces; unquoted passwords use a restricted alphabet.
Labels, alphabets and minimum lengths limit noise; low entropy is never an exemption.

This is heuristic coverage: passwords below those minimums, unlabelled random strings,
other key/token formats or labels, escaped/split values and some unquoted punctuation
may escape it. Base64-looking key and Basic values are detected as written, not decoded;
arbitrary encoded/compressed contents and text rendered in images are not inspected.
BR-333 covers image review separately. No scanner result proves the absence of every secret.

Each allowlist row names exactly a path, detector, value fingerprint and reviewed
reason. Repeated occurrences of that value in that path share a row. A different
value fails even in the same file; a row with no remaining match fails. Never add a
path-wide exemption or automatically accept every finding. The initial rows cover
protocol field names, paths, enums, explicit placeholders and synthetic test inputs.
To run identical detector code in the pinned runtime: `make qa-secrets SECRETS_RUNTIME=image`.
The image must already exist locally: `--pull=never --network=none --read-only`, all
capabilities dropped, numeric unprivileged user. Only the checksum-validated scanner
source is mounted; tracked bytes arrive on stdin. Git enumeration remains on the host;
the container returns only coordinates and fingerprints. Docker execution is an
orchestrator check; local tests exercise the exact stdin/stdout protocol natively.

# History handoff

The orchestrator first obtains a fresh **mirror clone** of the private forge, with
all branches, tags, notes and `refs/pull/*` advertised and fetched, then attests its
completeness. Nothing here contacts the forge or refreshes the mirror:

```sh
python scripts/history_secrets.py /path/to/mirror.git
python scripts/history_secrets.py /path/to/mirror.git --check-verdicts
```

The first command refuses shallow/non-mirror repositories and a mirror without
pull-request refs. It scans each commit reachable from all refs, including deleted
files and the workshop; each distinct blob is scanned once and findings are mapped
back to every commit/path. A submodule requires its own history scan. Ref/commit and
candidate/fingerprint counts appear in the report, alongside the actual native Python
implementation and version. History scanning has no image mode. The tool cannot attest that a
local mirror is current or that the forge advertises all hidden refs.

Outputs are `HULL-98-history-scan.md` and `HULL-98-history-verdicts.md` in the workshop
acts directory. Existing reports are never overwritten. An allowlist fingerprint
inherits `not a credential` and its reason; every other fingerprint starts `PENDING`.
Only the operator may resolve it as `not a credential`, `not live`, or `rotated`, with
its date. The two candidates recorded on 2026-11-14 must be reconciled by the operator.
No real history scan or verdict is claimed by fixture tests.

# Export rehearsal

```sh
python scripts/export_rehearsal.py /path/to/new-scratch-directory --ref HEAD --cairn cairn
```

The script uses `git archive`, constructs a new one-commit repository, and runs
`make gate`, `make qa`, and both Cairn readings there. It copies no workshop, local
context, authentication directory or MCP configuration into the export. The four
public ignore entries are added to the export's `.gitignore`, not the private source.
Only the public context example and project Dockerfile remain tracked under `.cairn/`.
Local settings are temporarily merged with the example's public contract for
INV-069 and INV-070, under mode 0600,
never staged, then deleted even if checks fail. Cairn output is reduced to invariant
statuses so a failed host check cannot expose its local values.

`--checks non-docker` uses the installed Python tools, unit/integration tests, guards
and `qa-secrets`, with no dependency downloads or analysis-service calls. It is not
a full `GATE: OK` or `make qa` claim. `--checks prepare` only exports. A tree object
can be supplied for pre-commit validation; final acceptance must replay the actual
committed head/tag. The script reports overall KO for any Cairn failure, even when
INV-068 through INV-070 pass; open inline-rule deltas are not hidden or exempted.
