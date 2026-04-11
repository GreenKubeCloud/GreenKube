#!/usr/bin/env bash
# pg_upgrade_17_to_18.sh
#
# Upgrades the GreenKube PostgreSQL data directory from version 17 to 18
# in-place on a Kubernetes cluster using a temporary Job.
#
# Usage:
#   ./scripts/pg_upgrade_17_to_18.sh [NAMESPACE]
#
# Prerequisites:
#   - kubectl configured against the target cluster
#   - The greenkube Helm release must be uninstalled (postgres pod stopped)
#     so the PVC is free
#   - The PVC "data-greenkube-postgres-0" must exist in the namespace
#
# The script:
#   1. Launches a Job that mounts the existing PVC
#   2. Installs pg17 server binaries via apk (runs as root)
#   3. Initialises a fresh PG18 cluster in pgdata_new
#   4. Runs pg_upgrade --link (instant, no data copy)
#   5. Swaps directories so the PVC holds PG18 data
#   6. Old data is backed up as pgdata_pg17_bak
#
# After this script succeeds, re-install the Helm chart normally.

set -euo pipefail

NAMESPACE="${1:-greenkube}"
JOB_NAME="postgres-upgrade-17-18"
PVC_NAME="data-greenkube-postgres-0"

echo "==> Checking prerequisites..."
kubectl get pvc "${PVC_NAME}" -n "${NAMESPACE}" > /dev/null

# Make sure no postgres pod is running (PVC must be free)
if kubectl get pod -n "${NAMESPACE}" -l app.kubernetes.io/component=postgres 2>/dev/null | grep -q Running; then
    echo "ERROR: A postgres pod is still running. Stop the Helm release first:"
    echo "  helm uninstall greenkube -n ${NAMESPACE}"
    exit 1
fi

echo "==> Deleting any previous upgrade job..."
kubectl delete job "${JOB_NAME}" -n "${NAMESPACE}" --ignore-not-found=true
kubectl wait --for=delete job/"${JOB_NAME}" -n "${NAMESPACE}" --timeout=60s 2>/dev/null || true

echo "==> Creating pg_upgrade Job..."
cat <<'JOBEOF' | sed "s/@@PVC_NAME@@/${PVC_NAME}/g" | kubectl apply -n "${NAMESPACE}" -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: postgres-upgrade-17-18
spec:
  ttlSecondsAfterFinished: 3600
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      # Run as root so we can install pg17 binaries and chmod.
      # pg_upgrade itself is invoked via `su postgres`.
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
          securityContext:
            runAsUser: 0
          command:
            - /bin/sh
            - -c
            - |
              set -ex

              OLD_DATA=/var/lib/postgresql/data/pgdata
              NEW_DATA=/var/lib/postgresql/data/pgdata_new

              # Verify the old data directory is PG17
              OLD_VERSION=$(cat "${OLD_DATA}/PG_VERSION" 2>/dev/null || echo "unknown")
              echo "Old cluster version: ${OLD_VERSION}"
              if [ "${OLD_VERSION}" != "17" ]; then
                echo "ERROR: Expected PG17, found '${OLD_VERSION}'. Aborting."
                exit 1
              fi

              # Install pg17 server binaries (needed by pg_upgrade to read old format)
              apk add --no-cache postgresql17 postgresql17-contrib

              # Alpine puts pg binaries under /usr/libexec/postgresql{17,}
              OLD_BIN=/usr/libexec/postgresql17
              NEW_BIN=/usr/libexec/postgresql

              # Create a temp password file for initdb
              PWFILE=/var/lib/postgresql/data/.pgpassword
              grep -q 'POSTGRES_PASSWORD' /proc/1/environ 2>/dev/null && \
                cat /proc/1/environ | tr '\0' '\n' | grep '^POSTGRES_PASSWORD=' | cut -d= -f2 > "${PWFILE}" || \
                echo "changeme" > "${PWFILE}"
              chown 70:70 "${PWFILE}"
              chmod 600 "${PWFILE}"

              # Initialize a fresh PG18 cluster and run pg_upgrade as postgres (UID 70)
              mkdir -p "${NEW_DATA}"
              chown -R 70:70 "${NEW_DATA}"

              su postgres -s /bin/sh -c "
                set -ex
                ${NEW_BIN}/initdb \
                  --auth-host=scram-sha-256 \
                  --auth-local=trust \
                  --pwfile=${PWFILE} \
                  -D ${NEW_DATA} \
                  --username=greenkube

                cd /tmp
                ${NEW_BIN}/pg_upgrade \
                  --old-datadir=${OLD_DATA} \
                  --new-datadir=${NEW_DATA} \
                  --old-bindir=${OLD_BIN} \
                  --new-bindir=${NEW_BIN} \
                  --username=greenkube \
                  --link
              "

              # Clean up temp password file
              rm -f "${PWFILE}"

              # Atomic swap: old → backup, new → active
              mv "${OLD_DATA}" /var/lib/postgresql/data/pgdata_pg17_bak
              mv "${NEW_DATA}" "${OLD_DATA}"

              echo "=== UPGRADE COMPLETE ==="
              echo "PG_VERSION: $(cat ${OLD_DATA}/PG_VERSION)"
          volumeMounts:
            - name: pgdata
              mountPath: /var/lib/postgresql/data
            - name: pg-run
              mountPath: /var/run/postgresql
            - name: tmp
              mountPath: /tmp
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "1"
              memory: "1Gi"
JOBEOF

echo "==> Waiting for pg_upgrade Job to complete (timeout: 10m)..."
kubectl wait --for=condition=complete job/"${JOB_NAME}" -n "${NAMESPACE}" --timeout=600s

echo ""
echo "==> pg_upgrade Job logs:"
kubectl logs -n "${NAMESPACE}" -l job-name="${JOB_NAME}" --tail=20

echo ""
echo "✅ PostgreSQL data successfully upgraded from 17 → 18."
echo "   Old data backed up inside the PVC at: pgdata_pg17_bak/"
echo ""
echo "Next steps:"
echo "  1. Re-install the Helm chart:"
echo "     helm install greenkube ./helm-chart -n ${NAMESPACE} --set postgres.image.tag=18-alpine ..."
echo "  2. Once verified, delete the backup:"
echo "     kubectl exec -n ${NAMESPACE} greenkube-postgres-0 -- rm -rf /var/lib/postgresql/data/pgdata_pg17_bak"
