# Operations

The SQLite ledger is authoritative. Slack delivery jobs are separately observable:

```sh
docker exec karma-x python -m karma_x.admin status --db /data/karma.db
docker logs --since 15m karma-x
```

`pending` jobs retry known transient failures. `blocked` usually means missing permission.
`uncertain` posts may already exist and are never automatically sent again. `failed` replies may
require rerunning a read command. Only an explicit private-reply 429 is automatically retried;
its complete reply is cached, so retrying it cannot create another award.

Native-table replies are private `chat.postEphemeral` messages. Text replies use the response URL.
HTTPX and HTTP core logs are restricted to warnings to avoid logging credential-bearing hook URLs.

## Recover delivery

1. Fix the reported Slack scope, role, token, or membership error privately; restart when tokens change.
2. Retry a safe name/message update: `docker exec karma-x python -m karma_x.admin retry-effect K_ID profile` (or `update` for a canceled recognition).
3. For an uncertain **post**, find its exact recognition ID in the original Slack channel.
4. Attach the already-existing bot message: `docker exec karma-x python -m karma_x.admin attach-post K_ID EXACT_SLACK_TIMESTAMP`.
5. Verify the thread and current name from Slack. If no post can be found, leave it uncertain or cancel the recognition; do not replay an unknown external write.

The attach tool reads the exact timestamp and checks the bot author plus the final
`Recognition ID: K…` line, which the bot appends after the reason. It does not rely on message
metadata or accept an ID embedded in a reason. It sends no new recognition. If canceled meanwhile,
it queues the existing message update. These are local operator tools, not public admin routes.

## Backup

Use a new UTC filename and SQLite's backup API:

```sh
docker exec karma-x python -m karma_x.admin backup \
  --db /data/karma.db --output /data/backups/2026-10-10T120000Z.db
```

Never copy a live `.db` file alone while WAL is active. Schedule hourly online backups and copy
the completed files to a private versioned Cloud Storage bucket using the VM service account.
Restrict bucket access and retain seven days (168 hourly copies); the target recovery point is
one hour. The operator must configure this schedule and verify uploads plus a restore.
Recognition reasons and queued response URLs are private; never attach the backup to GitHub or Slack.
Completed/terminal requests remove their response URLs. Deploys also create a pre-change backup.

## Restore

1. Verify a selected backup with `PRAGMA integrity_check` and compare recognition/user totals in a temporary directory.
2. Stop `karma-x`, preserving its current data directory as the rollback copy.
3. Restore the selected database into a **new empty** data directory with UID/GID 10001 and mode 600; do not reuse old WAL/SHM files.
4. Update the container bind mount to that directory, start one writer, and verify counts plus an existing thread in Slack.
5. Reconcile uncertain deliveries before enabling awards; a restored old ledger can disagree with later Slack posts/name edits.

The included deployment script always mounts `/var/lib/karma-x/data`, so move the verified restored
directory into that location only after preserving the prior directory and obtaining restore approval.
Image rollback preserves the current database; database restore is a separate intentional action.

One VM is not high availability. Alert on failed health checks, disk usage above 80%, persistent
pending jobs, and nonzero blocked/uncertain effects. Logs use error codes and IDs, not request bodies,
tokens, reasons, or response URLs. A cloud restore drill and reboot test are required before calling
the deployment production-ready.
