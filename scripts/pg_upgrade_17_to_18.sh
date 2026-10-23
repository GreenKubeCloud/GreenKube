#!/usr/bin/env bash
# Upgrade a GreenKube PostgreSQL 17 PVC to PostgreSQL 18.
#
# The operation is deliberately non-interactive.  A backup command and a
# separate verification command must be supplied by the operator:
#   BACKUP_COMMAND='pg_dump ... > /secure/location/greenkube.sql'
#   BACKUP_VERIFY_COMMAND='test -s /secure/location/greenkube.sql'
#
# Usage:
#   BACKUP_COMMAND=... BACKUP_VERIFY_COMMAND=... ./scripts/pg_upgrade_17_to_18.sh [NAMESPACE]
#   ACTION=rollback ./scripts/pg_upgrade_17_to_18.sh [NAMESPACE]
#
# Environment:
#   PVC_NAME (data-greenkube-postgres-0), SECRET_NAME (greenkube),
#   SECRET_KEY (POSTGRES_PASSWORD), POSTGRES_ROLE (greenkube),
#   JOB_NAME (postgres-upgrade-17-18), KUBECTL_BIN (kubectl),
#   ACTION (upgrade), BACKUP_COMMAND, BACKUP_VERIFY_COMMAND.

set -euo pipefail

NAMESPACE="${1:-${NAMESPACE:-greenkube}}"
PVC_NAME="${PVC_NAME:-data-greenkube-postgres-0}"
SECRET_NAME="${SECRET_NAME:-greenkube}"
SECRET_KEY="${SECRET_KEY:-POSTGRES_PASSWORD}"
POSTGRES_ROLE="${POSTGRES_ROLE:-greenkube}"
JOB_NAME="${JOB_NAME:-postgres-upgrade-17-18}"
KUBECTL_BIN="${KUBECTL_BIN:-kubectl}"
ACTION="${ACTION:-upgrade}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*"; }
valid_name() { [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; }

command -v "$KUBECTL_BIN" >/dev/null 2>&1 || die "kubectl was not found (set KUBECTL_BIN to a testable command)."
[[ "$ACTION" == upgrade || "$ACTION" == rollback ]] || die "ACTION must be upgrade or rollback."
valid_name "$NAMESPACE" || die "Invalid namespace."
valid_name "$PVC_NAME" || die "Invalid PVC name."
valid_name "$SECRET_NAME" || die "Invalid Secret name."
valid_name "$POSTGRES_ROLE" || die "Invalid PostgreSQL role."
valid_name "$JOB_NAME" || die "Invalid Job name."

log "Checking cluster and PostgreSQL Secret prerequisites..."
"$KUBECTL_BIN" get namespace "$NAMESPACE" >/dev/null
"$KUBECTL_BIN" get pvc "$PVC_NAME" -n "$NAMESPACE" >/dev/null
SECRET_VALUE="$("$KUBECTL_BIN" get secret "$SECRET_NAME" -n "$NAMESPACE" \
  -o "jsonpath={.data.${SECRET_KEY}}" 2>/dev/null)" || die "Secret ${SECRET_NAME} or key ${SECRET_KEY} is unavailable."
[[ -n "$SECRET_VALUE" ]] || die "Secret ${SECRET_NAME} key ${SECRET_KEY} is empty."
if ! printf '%s' "$SECRET_VALUE" | (base64 -D >/dev/null 2>/dev/null || base64 -d >/dev/null 2>/dev/null); then
  die "Secret ${SECRET_NAME} key ${SECRET_KEY} is not valid base64."
fi

if [[ "$ACTION" == upgrade ]]; then
  [[ -n "${BACKUP_COMMAND:-}" ]] || die "BACKUP_COMMAND is required; no external backup was supplied."
  [[ -n "${BACKUP_VERIFY_COMMAND:-}" ]] || die "BACKUP_VERIFY_COMMAND is required."
  log "Creating external backup (output is not echoed)..."
  /bin/sh -c "$BACKUP_COMMAND" || die "External backup command failed."
  log "Verifying external backup..."
  /bin/sh -c "$BACKUP_VERIFY_COMMAND" || die "External backup verification failed."
fi

if "$KUBECTL_BIN" get pod -n "$NAMESPACE" -l app.kubernetes.io/component=postgres \
  -o jsonpath='{range .items[*]}{.status.phase}{"\n"}{end}' 2>/dev/null | grep -qx Running; then
  die "A PostgreSQL pod is still running; stop the Helm release before proceeding."
fi

log "Replacing any previous upgrade Job..."
"$KUBECTL_BIN" delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found=true >/dev/null
"$KUBECTL_BIN" wait --for=delete "job/${JOB_NAME}" -n "$NAMESPACE" --timeout=60s >/dev/null 2>&1 || true

log "Creating ${ACTION} Job..."
cat <<'JOBEOF' | sed \
  -e "s/@@PVC_NAME@@/${PVC_NAME}/g" \
  -e "s/@@SECRET_NAME@@/${SECRET_NAME}/g" \
  -e "s/@@SECRET_KEY@@/${SECRET_KEY}/g" \
  -e "s/@@POSTGRES_ROLE@@/${POSTGRES_ROLE}/g" \
  -e "s/@@ACTION@@/${ACTION}/g" \
  -e "s/@@JOB_NAME@@/${JOB_NAME}/g" | "$KUBECTL_BIN" apply -n "$NAMESPACE" -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: @@JOB_NAME@@
spec:
  ttlSecondsAfterFinished: 3600
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        fsGroup: 70
      volumes:
        - name: pgdata
          persistentVolumeClaim:
            claimName: @@PVC_NAME@@
        - name: pg-run
          emptyDir:
            sizeLimit: 16Mi
        - name: tmp
          emptyDir:
            sizeLimit: 256Mi
      containers:
        - name: pg-upgrade
          image: postgres:18-alpine
          imagePullPolicy: IfNotPresent
          env:
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: @@SECRET_NAME@@
                  key: @@SECRET_KEY@@
          securityContext:
            runAsUser: 0
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              OLD_DATA=/var/lib/postgresql/data/pgdata
              NEW_DATA=/var/lib/postgresql/data/pgdata_new
              BACKUP_DATA=/var/lib/postgresql/data/pgdata_pg17_bak
              if [ "@@ACTION@@" = rollback ]; then
                test -f "$OLD_DATA/PG_VERSION" && test -f "$BACKUP_DATA/PG_VERSION"
                test "$(cat "$OLD_DATA/PG_VERSION")" = 18
                test "$(cat "$BACKUP_DATA/PG_VERSION")" = 17
                mv "$OLD_DATA" "${OLD_DATA}.rollback"
                mv "$BACKUP_DATA" "$OLD_DATA"
                mv "${OLD_DATA}.rollback" "$BACKUP_DATA"
                test "$(cat "$OLD_DATA/PG_VERSION")" = 17
                echo "ROLLBACK COMPLETE"
                exit 0
              fi
              test -f "$OLD_DATA/PG_VERSION"
              test "$(cat "$OLD_DATA/PG_VERSION")" = 17
              test -n "${POSTGRES_PASSWORD:-}"
              apk add --no-cache postgresql17 postgresql17-contrib
              OLD_BIN=/usr/libexec/postgresql17
              NEW_BIN=/usr/libexec/postgresql
              PWFILE=/var/lib/postgresql/data/.pgpassword
              trap 'rm -f "$PWFILE"' EXIT
              printf '%s' "$POSTGRES_PASSWORD" > "$PWFILE"
              chown 70:70 "$PWFILE"
              chmod 600 "$PWFILE"
              rm -rf "$NEW_DATA"
              mkdir -p "$NEW_DATA"
              chown -R 70:70 "$NEW_DATA"
              su postgres -s /bin/sh -c "
                set -eu
                ${NEW_BIN}/initdb --auth-host=scram-sha-256 --auth-local=trust \
                  --pwfile=${PWFILE} -D ${NEW_DATA} --username=@@POSTGRES_ROLE@@
                cd /tmp
                ${NEW_BIN}/pg_upgrade --old-datadir=${OLD_DATA} --new-datadir=${NEW_DATA} \
                  --old-bindir=${OLD_BIN} --new-bindir=${NEW_BIN} \
                  --username=@@POSTGRES_ROLE@@ --link
              "
              test ! -e "$BACKUP_DATA"
              mv "$OLD_DATA" "$BACKUP_DATA"
              mv "$NEW_DATA" "$OLD_DATA"
              test "$(cat "$OLD_DATA/PG_VERSION")" = 18
              test "$(cat "$BACKUP_DATA/PG_VERSION")" = 17
              echo "UPGRADE COMPLETE"
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
            - name: pg-run
              mountPath: /var/run/postgresql
            - name: tmp
              mountPath: /tmp
JOBEOF

log "Waiting for ${ACTION} Job (timeout: 10m)..."
"$KUBECTL_BIN" wait --for=condition=complete "job/${JOB_NAME}" -n "$NAMESPACE" --timeout=600s
"$KUBECTL_BIN" logs -n "$NAMESPACE" "job/${JOB_NAME}" --tail=20 | sed -E \
  "s/(POSTGRES_PASSWORD|password|postgresql:\/\/[^ ]+)/[REDACTED]/gI"
log "Job completed successfully (${ACTION})."
