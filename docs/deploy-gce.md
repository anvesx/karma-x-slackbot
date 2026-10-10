# Deployment owner: Helly

Target: **one GCE VM + SQLite on a separate ext4 Persistent Disk**. Do not deploy this
container to a service that discards its filesystem or adds concurrent replicas.
Cloud resources and deployment have not been executed by this repo's local validation.

## Provision once

1. Create an Ubuntu LTS VM (one non-preemptible instance; start with 2 vCPU/2 GiB),
   one 10 GiB Persistent Disk in the same zone, and an Artifact Registry Docker repository.
2. Attach the disk with auto-delete disabled; format only the **new empty** disk as ext4,
   mount at `/var/lib/karma-x`, and persist the UUID mount in `/etc/fstab`.
3. Install Docker, Google Cloud CLI, and Caddy from their official sources. Add Docker's
   systemd drop-in `RequiresMountsFor=/var/lib/karma-x`, reload units, and enable Docker.
4. Give the VM service account read access to this Artifact Registry repo and configure
   root's Docker credential helper for `REGION-docker.pkg.dev`. Use the VM identity, no JSON key.
5. Assign a stable domain/static address; allow inbound 80/443 for Caddy and 22 only from
   IAP's `35.235.240.0/20`. Enable OS Login. Configure HTTPS using `deploy/Caddyfile.example`.

The disk must be a real mount point before `karma-x-deploy` runs; the script fails if it is missing.
Use `/var/lib/karma-x/data` for the database and owned UID/GID 10001. Back up outside the VM as well.
Public HTTPS is needed for Slack's signed callbacks; SSH and the app's 8790 port stay restricted.

## Install runtime configuration

Create `/etc/karma-x` root-owned mode 700. Install both files root-owned mode 600:

`/etc/karma-x/karma-x.env`:

```dotenv
SLACK_SIGNING_SECRET=FILL_PRIVATELY
SLACK_BOT_TOKEN=FILL_PRIVATELY
SLACK_PROFILE_TOKEN=FILL_PRIVATELY_WITH_AUTHORIZED_USER_TOKEN
SLACK_TEAM_ID=FILL_WORKSPACE_ID
ALLOWED_CHANNEL_IDS=FILL_APPROVED_CHANNEL_IDS
DEFAULT_TIMEZONE=Asia/Kolkata
DATABASE_PATH=/data/karma.db
HOST=0.0.0.0
PORT=8790
```

`/etc/karma-x/deploy.conf` (trusted Bash configuration):

```sh
IMAGE_REPOSITORY=REGION-docker.pkg.dev/PROJECT/REGISTRY/karma-x
```

Install the reviewed `deploy/deploy.sh` root-owned mode 755 at `/usr/local/sbin/karma-x-deploy`.
The workflow invokes this installed script; updating the script itself is an operator action.
Invite the installed Slack app into the initial test channel. Put the stable HTTPS
`https://YOUR_DOMAIN/slack/commands` in the Slack manifest; reinstall only if scopes change.

## GitHub Actions

Create the `production` environment. Require an approver for the first deployment.
Set repository variables:

| Variable | Value |
| --- | --- |
| `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE` | Your chosen project, image region, and VM zone. |
| `GCP_REGISTRY`, `GCP_VM` | Artifact Registry repository and VM names. |
| `GCP_WIF_PROVIDER` | Full Workload Identity provider resource name. |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Dedicated deploy service-account email. |
| `DEPLOY_ENABLED` | Leave unset until ready; set `true` to deploy passing main changes. |

Configure GitHub OIDC → Workload Identity Federation. Restrict trust to the numeric owner/repo
IDs for **anvesx/karma-x-slackbot** and `refs/heads/main`; do not trust arbitrary GitHub subjects
or fork PRs. Grant that principal service-account impersonation. The deploy identity needs
Artifact Registry writer on this repo, IAP tunnel access scoped to this VM, OS Admin Login,
VM metadata read permissions needed by `gcloud compute ssh`, and Service Account User on the VM's
attached service account. The VM identity only needs image-reader access plus the chosen backup
destination. Avoid project-wide Editor/Owner and stored service-account keys.

The workflow runs `make check` and a Docker build first. It builds/pushes an image tagged with
the commit, resolves its digest, and invokes deployment through IAP. Both Actions concurrency
and a VM `flock` serialize deploys. The script pulls before stopping the old container, creates
an online SQLite backup, stops the sole writer, starts the new digest, and polls health.
Startup failure restores the previous container while retaining the database. This is a short
downtime deployment; additive schema changes are required for safe image rollback.

## Deployment acceptance

1. Run the workflow manually with `DEPLOY_ENABLED=true` and verify its check/deploy jobs succeed.
2. Run one live award, list-all, each winner query, view, cancel, and repeated cancel in the test channel.
3. Verify the actual `Person 🌟N` display name, a clickable thread, and the local-date/UTC boundary.
4. Restart the container and reboot the VM; verify the same counts persist and the disk is mounted.
5. Restore a backup into a temporary directory; verify SQLite integrity and ledger totals before approving production channels.

Reference: [Persistent Disk](https://docs.cloud.google.com/compute/docs/disks/persistent-disks),
[Artifact Registry Docker authentication](https://docs.cloud.google.com/artifact-registry/docs/docker/authentication),
[GitHub OIDC auth action](https://github.com/google-github-actions/auth),
[IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding).
