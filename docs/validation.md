# Validation evidence

Validation date: 2026-10-10. The app is installed and was exercised through real Slack commands
against the local server in the existing isolated test channel. The actual display-name suffix
is **blocked**: Slack returned `not_admin` for the installing ordinary member's user token.
Cloud deployment belongs to Helly and has not been run.

| Layer | State | Evidence |
| --- | --- | --- |
| Parser, UTC/DST boundaries, SQLite transactions and restart | Passed | `make check`: 98 passed, 88% statement coverage; temporary SQLite databases. |
| Signed HTTP route to durable worker to simulated Slack | Passed | FastAPI tests; verifier also ran real uvicorn and the installed Slack SDK against an HTTP stub. |
| Failure after remote name write; duplicate POST risk; foreign user; malformed signature timestamp | Passed | Regression tests first failed, then passed; independently reproduced fixes. |
| Container build and restart/volume persistence | Passed locally | Nonroot UID 10001, read-only root, signed self-block, unsigned 401, total 2 survives restart, backup restores total 2. |
| Real Slack awards and self-recognition rejection | Passed | Four deliberate awards produced two recipients with 2 each; a selected self-mention was rejected without a ledger write. |
| Real Slack table and recognition thread | Passed | Actual `list-all` rendered a private native table with shared ranks and clickable links; one link opened the exact recognition and its thread. The 30-row cap uses a 31-person local fixture. |
| Real Slack winner and view commands | Passed | Day/month/year/custom queries returned both tied winners in the caller's timezone; history and single-ID view returned the stored UTC timestamps, reasons, and links. Empty/reversed date ranges were exercised. |
| Real Slack cancellation | Passed | Cancel decremented once and marked the original message canceled; repeat stayed at 1; the other recipient became the sole winner. All four test awards were canceled afterward: zero active records/totals, audit retained. |
| Actual recipient display name `Person 🌟2` | Blocked | Real profile API returned `not_admin`; the supplied profile token belongs to an ordinary member. Owner/Admin access and a live name readback are still required. |
| GCE, DNS/TLS, GitHub Actions deploy and cloud restore | Not run | Helly's deployment checklist. |

## Reproduce local gates

```sh
uv sync --frozen
make check
docker build -t karma-x:local .
uv run --frozen python scripts/smoke_container.py karma-x:local
actionlint .github/workflows/ci.yml
shellcheck deploy/deploy.sh
```

Observed exits: all 0. `make check` ended with `98 passed, 1 warning in 2.54s`; the warning is
Starlette's HTTPX TestClient deprecation, not a failed test. The container smoke ended with:
`PASS: signed self-block, unsigned401, UID10001/read-only, persistent total2 after restart, restored backup total2`.

The defect scan checked 21 Python/shell files: 0 FAIL findings, one D10 advisory. UUID generation
at the storage boundary and unique smoke-test names/signature timestamps are intentional I/O,
not random or clock reads inside the pure parser/date logic. The publication scan checked all
39 staged deliverable files and found none of the runtime credentials.

Real HTTP checks returned 401 for an unsigned command and 413 for an oversized body; neither
changed the recognition count. Live creation timestamps were SQLite integers, and the ledger's
`PRAGMA integrity_check` returned `ok` after cleanup.

The local SQLite contention benchmark attempted 2,000 writes with eight writers: 1,998 committed,
two busy rejections, 1.103 seconds elapsed (1,812.2 commits/second), 5.07 ms P95 across all attempts;
top-30 lookup 1.22 ms, winner query 1.98 ms. This is local SQLite evidence, not Slack or GCE throughput.

An independent verifier reproduced the earlier profile/crash, post ambiguity, foreign-user,
historical-channel access, response retry, and recovery-ID fixes. The final SDK wire-format,
private-table delivery, and logging changes are awaiting the final independent review.

Unit and HTTP-stub results do not prove Slack permissions, rendering, or cloud deployment.
Real Slack screenshots and the test ledger stay local; no credentials, private channel contents,
personal profiles, or real database are published.
