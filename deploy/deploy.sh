#!/usr/bin/env bash
# Installed root-owned as /usr/local/sbin/karma-x-deploy. One local Persistent Disk.
set -euo pipefail
umask 077

[[ "$EUID" -eq 0 ]] || { echo 'Run via sudo on the GCE VM.' >&2; exit 1; }
[[ "$#" -eq 1 ]] || { echo 'Usage: karma-x-deploy repository@sha256:digest' >&2; exit 1; }
[[ -f /etc/karma-x/deploy.conf && -f /etc/karma-x/karma-x.env ]] || {
  echo 'Missing root-owned deployment configuration or runtime secrets.' >&2; exit 1;
}
for config in /etc/karma-x/deploy.conf /etc/karma-x/karma-x.env; do
  [[ "$(stat -c '%u:%a' "$config")" == '0:600' ]] || {
    echo 'Configuration must be root-owned and mode 600.' >&2; exit 1;
  }
done
# Trusted local configuration installed by the operator; never fetched from a pull request.
# shellcheck source=/dev/null
source /etc/karma-x/deploy.conf
: "${IMAGE_REPOSITORY:?Set IMAGE_REPOSITORY in deploy.conf}"
image="$1"
[[ "$image" == "$IMAGE_REPOSITORY"@sha256:* && "${image##*@sha256:}" =~ ^[a-f0-9]{64}$ ]] || {
  echo 'Use an immutable digest from the configured Artifact Registry repository.' >&2; exit 1;
}
mountpoint -q /var/lib/karma-x || { echo 'Persistent Disk is not mounted.' >&2; exit 1; }
mkdir -p /var/lib/karma-x/data
chown 10001:10001 /var/lib/karma-x/data
chmod 700 /var/lib/karma-x/data
exec 9>/var/lib/karma-x/deploy.lock
flock -n 9 || { echo 'Another deployment is in progress.' >&2; exit 1; }

docker pull "$image"
had_previous=false
if docker container inspect karma-x >/dev/null 2>&1; then
  # SQLite's backup API includes committed WAL pages; copying a running .db alone is unsafe.
  backup_name="/data/backups/predeploy-$(date -u +%Y%m%dT%H%M%S)-${image##*@sha256:}.db"
  docker exec karma-x python -m karma_x.admin backup --db /data/karma.db --output "$backup_name"
  if docker container inspect karma-x-previous >/dev/null 2>&1; then
    docker rm karma-x-previous >/dev/null # only the old, stopped container; no volumes
  fi
  docker stop --time 30 karma-x >/dev/null
  if ! docker rename karma-x karma-x-previous; then
    docker start karma-x >/dev/null
    echo 'Container rename failed; existing container restarted.' >&2
    exit 1
  fi
  had_previous=true
fi

rollback() {
  docker rm -f karma-x >/dev/null 2>&1 || true # container only; bind-mounted ledger is retained
  if [[ "$had_previous" == true ]]; then
    docker rename karma-x-previous karma-x
    docker start karma-x >/dev/null
    echo 'Previous container restored; database retained. Inspect logs and health.' >&2
  fi
}
if ! docker run -d --name karma-x --restart unless-stopped --stop-timeout 30 \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m --memory 512m --cpus 1 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --env-file /etc/karma-x/karma-x.env \
  --mount type=bind,src=/var/lib/karma-x/data,dst=/data \
  -p 127.0.0.1:8790:8790 "$image" >/dev/null; then
  rollback
  exit 1
fi

for _attempt in {1..20}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8790/health >/dev/null; then
    printf '%s\n' "$image" >/etc/karma-x/current-image
    echo 'Deployment healthy; immutable image and existing SQLite ledger are active.'
    exit 0
  fi
  sleep 2
done
echo 'New container failed 20 health attempts (up to about 80 seconds).' >&2
rollback
exit 1
