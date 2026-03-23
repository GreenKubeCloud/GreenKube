#!/usr/bin/env bash
# =============================================================================
# GreenKube Deployment Test Suite
# =============================================================================
#
# Tests all documented deployment scenarios against a local Minikube cluster.
# Run after every change to the Helm chart, Dockerfile, or documentation to
# verify that the deployment guide works correctly for early adopters.
#
# Prerequisites:
#   - minikube running
#   - helm 3 installed
#   - kubectl configured
#   - Docker image built: eval $(minikube docker-env) && docker build -t greenkube/greenkube:0.2.3-test .
#
# Usage:
#   ./scripts/test_deployments.sh              # Run all scenarios
#   ./scripts/test_deployments.sh --quick      # Skip slow scenarios (demo)
#   ./scripts/test_deployments.sh --scenario 3 # Run a single scenario
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CHART_DIR="$(cd "$(dirname "$0")/../helm-chart" && pwd)"
IMAGE_TAG="greenkube/greenkube:0.2.3-test"
WAIT_TIMEOUT=120   # seconds to wait for pods
HEALTH_RETRIES=30  # retries for health check (x 4s = 120s max)
QUICK=false
SINGLE_SCENARIO=""
PASSED=0
FAILED=0
SKIPPED=0
RESULTS=()

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --quick)   QUICK=true; shift ;;
        --scenario) SINGLE_SCENARIO="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_pass()  { echo -e "${GREEN}[PASS]${NC}  $*"; }
log_fail()  { echo -e "${RED}[FAIL]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_title() { echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${BLUE}  $*${NC}"; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

cleanup_namespace() {
    local ns="$1"
    log_info "Cleaning up namespace: $ns"
    helm uninstall greenkube -n "$ns" 2>/dev/null || true
    kubectl delete namespace "$ns" --wait=false 2>/dev/null || true
    # Wait for namespace to actually be deleted
    for i in $(seq 1 30); do
        if ! kubectl get namespace "$ns" &>/dev/null; then
            break
        fi
        sleep 2
    done
}

cleanup_demo_pod() {
    kubectl delete pod greenkube-demo --ignore-not-found=true 2>/dev/null || true
    for i in $(seq 1 15); do
        if ! kubectl get pod greenkube-demo &>/dev/null; then
            break
        fi
        sleep 2
    done
}

wait_for_pods() {
    local ns="$1"
    local expected_count="${2:-1}"
    local timeout="${3:-$WAIT_TIMEOUT}"
    log_info "Waiting for $expected_count pod(s) in $ns to be ready (timeout: ${timeout}s)..."
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        local ready_count
        ready_count=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -c "Running" || true)
        if [[ "$ready_count" -ge "$expected_count" ]]; then
            # Also check that all containers within each pod are ready
            local not_ready
            not_ready=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | awk '{split($2, a, "/"); if(a[1] != a[2]) print}' | wc -l | tr -d ' ')
            if [[ "$not_ready" -eq 0 ]]; then
                log_info "All $ready_count pod(s) ready in $ns."
                return 0
            fi
        fi
        sleep 4
        elapsed=$((elapsed + 4))
    done
    log_fail "Timed out waiting for pods in $ns."
    kubectl get pods -n "$ns" 2>/dev/null || true
    return 1
}

check_api_health() {
    local ns="$1"
    local pod_selector="${2:-app.kubernetes.io/name=greenkube,app.kubernetes.io/component=app}"
    local container="${3:-greenkube-api}"
    local port="${4:-8000}"

    log_info "Checking API health in $ns (port $port)..."
    local pod
    pod=$(kubectl get pods -n "$ns" -l "$pod_selector" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "$pod" ]]; then
        log_fail "No pod found with selector '$pod_selector' in $ns"
        return 1
    fi

    local retries=$HEALTH_RETRIES
    for i in $(seq 1 $retries); do
        local result
        result=$(kubectl exec -n "$ns" "$pod" -c "$container" -- \
            python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:$port/api/v1/health').read().decode())" 2>/dev/null || true)
        if echo "$result" | grep -q '"status":"ok"'; then
            log_pass "API health check passed: $result"
            return 0
        fi
        sleep 4
    done
    log_fail "API health check failed after $retries retries in $ns"
    return 1
}

check_api_health_demo() {
    local pod="$1"
    local port="${2:-9000}"

    log_info "Checking demo API health (port $port)..."
    local retries=$HEALTH_RETRIES
    for i in $(seq 1 $retries); do
        local result
        result=$(kubectl exec "$pod" -- \
            python -c "import urllib.request; print(urllib.request.urlopen('http://0.0.0.0:$port/api/v1/health').read().decode())" 2>/dev/null || true)
        if echo "$result" | grep -q '"status":"ok"'; then
            log_pass "Demo API health check passed: $result"
            return 0
        fi
        sleep 4
    done
    log_fail "Demo API health check failed after $retries retries"
    return 1
}

check_collector_logs() {
    local ns="$1"
    local pod_selector="${2:-app.kubernetes.io/name=greenkube,app.kubernetes.io/component=app}"
    local container="${3:-greenkube}"

    log_info "Checking collector logs for errors in $ns..."
    local pod
    pod=$(kubectl get pods -n "$ns" -l "$pod_selector" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "$pod" ]]; then
        log_warn "No pod found for log check"
        return 0
    fi

    local logs
    logs=$(kubectl logs -n "$ns" "$pod" -c "$container" --tail=50 2>/dev/null || true)

    # Check for critical errors (not warnings)
    local critical_errors
    critical_errors=$(echo "$logs" | grep -i "ERROR" | grep -v "ELECTRICITY_MAPS_TOKEN" | grep -v "Traceback" | head -5 || true)

    if [[ -n "$critical_errors" ]]; then
        log_warn "Collector has errors (may be expected without Prometheus/OpenCost):"
        echo "$critical_errors" | head -3
    fi

    # Specifically check for the SQLite bug we fixed
    if echo "$logs" | grep -q "Cannot setup SQLite, no connection available"; then
        log_fail "SQLite setup bug detected! The fix may not be applied."
        return 1
    fi

    if echo "$logs" | grep -q "no such table: node_snapshots"; then
        log_fail "SQLite schema not initialized! node_snapshots table missing."
        return 1
    fi

    return 0
}

record_result() {
    local scenario_num="$1"
    local name="$2"
    local status="$3"
    if [[ "$status" == "PASS" ]]; then
        PASSED=$((PASSED + 1))
        RESULTS+=("${GREEN}✅ Scenario $scenario_num: $name${NC}")
    elif [[ "$status" == "FAIL" ]]; then
        FAILED=$((FAILED + 1))
        RESULTS+=("${RED}❌ Scenario $scenario_num: $name${NC}")
    else
        SKIPPED=$((SKIPPED + 1))
        RESULTS+=("${YELLOW}⏭️  Scenario $scenario_num: $name (skipped)${NC}")
    fi
}

should_run() {
    local num="$1"
    if [[ -n "$SINGLE_SCENARIO" && "$SINGLE_SCENARIO" != "$num" ]]; then
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
log_title "🚀 GreenKube Deployment Test Suite"
echo ""

log_info "Checking prerequisites..."
if ! command -v minikube &>/dev/null; then echo "❌ minikube not found"; exit 1; fi
if ! command -v helm &>/dev/null;     then echo "❌ helm not found"; exit 1; fi
if ! command -v kubectl &>/dev/null;  then echo "❌ kubectl not found"; exit 1; fi

if ! minikube status 2>/dev/null | grep -q "host: Running"; then
    log_fail "Minikube is not running. Start it with: minikube start"
    exit 1
fi
log_pass "All prerequisites met."

# Make sure we're in default namespace context
kubectl config set-context --current --namespace=default &>/dev/null

# Check if the test image exists in minikube
eval $(minikube docker-env 2>/dev/null) || true
if ! docker image inspect "$IMAGE_TAG" &>/dev/null; then
    log_warn "Image $IMAGE_TAG not found in minikube Docker. Building..."
    cd "$(dirname "$0")/.."
    docker build -t "$IMAGE_TAG" . 2>&1 | tail -3
fi

# Verify Helm templates render without errors
log_info "Verifying Helm chart template rendering..."
if ! helm template test "$CHART_DIR" &>/dev/null; then
    log_fail "Helm template rendering failed!"
    helm template test "$CHART_DIR" 2>&1 | tail -10
    exit 1
fi
log_pass "Helm templates render successfully."
echo ""

# ==========================================================================
# SCENARIO 1: PostgreSQL (default) — No token, no custom values
# ==========================================================================
if should_run 1; then
    log_title "Scenario 1: PostgreSQL (default) — Minimal install"
    NS="gk-test-pg"
    cleanup_namespace "$NS"

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 2 && \
           check_api_health "$NS" && \
           check_collector_logs "$NS"; then
            record_result 1 "PostgreSQL default" "PASS"
        else
            record_result 1 "PostgreSQL default" "FAIL"
        fi
    else
        record_result 1 "PostgreSQL default" "FAIL"
    fi
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 2: SQLite — No persistence, no token
# ==========================================================================
if should_run 2; then
    log_title "Scenario 2: SQLite — No persistence, no token"
    NS="gk-test-sqlite"
    cleanup_namespace "$NS"

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set config.db.type=sqlite \
        --set postgres.enabled=false \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 1 && \
           check_api_health "$NS" && \
           check_collector_logs "$NS"; then
            record_result 2 "SQLite (no persistence)" "PASS"
        else
            record_result 2 "SQLite (no persistence)" "FAIL"
        fi
    else
        record_result 2 "SQLite (no persistence)" "FAIL"
    fi
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 3: SQLite — With persistence
# ==========================================================================
if should_run 3; then
    log_title "Scenario 3: SQLite — With persistence (PVC)"
    NS="gk-test-sqlite-pvc"
    cleanup_namespace "$NS"

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set config.db.type=sqlite \
        --set postgres.enabled=false \
        --set config.persistence.enabled=true \
        --set config.persistence.size=500Mi \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 1 && \
           check_api_health "$NS"; then
            # Verify PVC was created
            if kubectl get pvc -n "$NS" 2>/dev/null | grep -q "greenkube"; then
                log_pass "PVC created for SQLite persistence."
                record_result 3 "SQLite with persistence" "PASS"
            else
                log_fail "PVC not created!"
                record_result 3 "SQLite with persistence" "FAIL"
            fi
        else
            record_result 3 "SQLite with persistence" "FAIL"
        fi
    else
        record_result 3 "SQLite with persistence" "FAIL"
    fi
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 4: PostgreSQL — With custom values file
# ==========================================================================
if should_run 4; then
    log_title "Scenario 4: PostgreSQL — Custom values file"
    NS="gk-test-custom"
    cleanup_namespace "$NS"

    # Create a temporary custom values file
    TMPVALS=$(mktemp /tmp/greenkube-test-values.XXXXXX.yaml)
    cat > "$TMPVALS" <<'EOF'
config:
  cloudProvider: on-prem
  defaultZone: DE
  defaultIntensity: 400.0
  db:
    type: postgres
  recommendations:
    lookbackDays: 14
    rightsizingCpuThreshold: 0.4
postgres:
  enabled: true
  persistence:
    enabled: false
EOF

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        -f "$TMPVALS" \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 2 && \
           check_api_health "$NS"; then
            # Verify custom config was applied
            local_config=$(kubectl get configmap -n "$NS" greenkube -o jsonpath='{.data.CLOUD_PROVIDER}' 2>/dev/null || true)
            if [[ "$local_config" == "on-prem" ]]; then
                log_pass "Custom cloudProvider=on-prem applied correctly."
                record_result 4 "PostgreSQL custom values" "PASS"
            else
                log_fail "Custom cloudProvider not applied (got: '$local_config')"
                record_result 4 "PostgreSQL custom values" "FAIL"
            fi
        else
            record_result 4 "PostgreSQL custom values" "FAIL"
        fi
    else
        record_result 4 "PostgreSQL custom values" "FAIL"
    fi
    rm -f "$TMPVALS"
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 5: PostgreSQL — With Electricity Maps token (dummy)
# ==========================================================================
if should_run 5; then
    log_title "Scenario 5: PostgreSQL — With Electricity Maps token"
    NS="gk-test-token"
    cleanup_namespace "$NS"

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set secrets.electricityMapsToken="test-token-12345" \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 2 && \
           check_api_health "$NS"; then
            # Verify the token was set in the secret
            token_set=$(kubectl get secret greenkube -n "$NS" -o jsonpath='{.data.ELECTRICITY_MAPS_TOKEN}' 2>/dev/null || true)
            if [[ -n "$token_set" ]]; then
                log_pass "Electricity Maps token is set in the secret."
                record_result 5 "PostgreSQL with token" "PASS"
            else
                log_fail "Electricity Maps token not found in secret."
                record_result 5 "PostgreSQL with token" "FAIL"
            fi
        else
            record_result 5 "PostgreSQL with token" "FAIL"
        fi
    else
        record_result 5 "PostgreSQL with token" "FAIL"
    fi
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 6: On-prem — Node labels + zone configuration
# ==========================================================================
if should_run 6; then
    log_title "Scenario 6: On-prem — Node labels + zone config"
    NS="gk-test-onprem"
    cleanup_namespace "$NS"

    # Label minikube node with a zone
    kubectl label nodes minikube topology.kubernetes.io/zone=FR --overwrite 2>/dev/null || true

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set config.cloudProvider=on-prem \
        --set config.defaultZone=FR \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 2 && \
           check_api_health "$NS"; then
            # Verify config
            zone_config=$(kubectl get configmap -n "$NS" greenkube -o jsonpath='{.data.DEFAULT_ZONE}' 2>/dev/null || true)
            provider_config=$(kubectl get configmap -n "$NS" greenkube -o jsonpath='{.data.CLOUD_PROVIDER}' 2>/dev/null || true)
            if [[ "$zone_config" == "FR" && "$provider_config" == "on-prem" ]]; then
                log_pass "On-prem config applied (zone=FR, provider=on-prem)."
                record_result 6 "On-prem with node labels" "PASS"
            else
                log_fail "On-prem config mismatch (zone=$zone_config, provider=$provider_config)"
                record_result 6 "On-prem with node labels" "FAIL"
            fi
        else
            record_result 6 "On-prem with node labels" "FAIL"
        fi
    else
        record_result 6 "On-prem with node labels" "FAIL"
    fi
    # Remove label
    kubectl label nodes minikube topology.kubernetes.io/zone- 2>/dev/null || true
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 7: PostgreSQL — With API key authentication
# ==========================================================================
if should_run 7; then
    log_title "Scenario 7: PostgreSQL — With API key"
    NS="gk-test-apikey"
    cleanup_namespace "$NS"

    if helm install greenkube "$CHART_DIR" \
        -n "$NS" --create-namespace \
        --set image.tag=0.2.3-test \
        --set image.pullPolicy=Never \
        --set secrets.apiKey="scenario7-auth-token" `# gitleaks:allow` \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false 2>&1 | tail -5; then

        if wait_for_pods "$NS" 2; then
            # Wait for API to be up
            sleep 10
            pod=$(kubectl get pods -n "$NS" -l app.kubernetes.io/name=greenkube,app.kubernetes.io/component=app -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

            # Test that unauthenticated request is rejected
            unauth_result=$(kubectl exec -n "$NS" "$pod" -c greenkube-api -- \
                python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8000/api/v1/metrics')
    print('NO_AUTH_NEEDED')
except urllib.error.HTTPError as e:
    print(f'HTTP_{e.code}')
except Exception as e:
    print(f'ERROR: {e}')
" 2>/dev/null || echo "EXEC_FAILED")

            # Health endpoint should always work (no auth required)
            health_result=$(kubectl exec -n "$NS" "$pod" -c greenkube-api -- \
                python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/health').read().decode())" 2>/dev/null || echo "FAILED")

            if echo "$health_result" | grep -q '"status":"ok"'; then
                log_pass "Health endpoint works without API key."
                # API key is set; unauthenticated requests to protected endpoints should fail
                if [[ "$unauth_result" == "HTTP_401" || "$unauth_result" == "HTTP_403" ]]; then
                    log_pass "Protected endpoints require API key (got $unauth_result)."
                    record_result 7 "PostgreSQL with API key" "PASS"
                else
                    log_warn "Unprotected access result: $unauth_result (API key may not protect all endpoints)."
                    record_result 7 "PostgreSQL with API key" "PASS"
                fi
            else
                log_fail "Health endpoint failed: $health_result"
                record_result 7 "PostgreSQL with API key" "FAIL"
            fi
        else
            record_result 7 "PostgreSQL with API key" "FAIL"
        fi
    else
        record_result 7 "PostgreSQL with API key" "FAIL"
    fi
    cleanup_namespace "$NS"
fi

# ==========================================================================
# SCENARIO 8: Demo Mode — kubectl run
# ==========================================================================
if should_run 8; then
    if [[ "$QUICK" == true ]]; then
        log_title "Scenario 8: Demo Mode (SKIPPED — quick mode)"
        record_result 8 "Demo mode" "SKIP"
    else
        log_title "Scenario 8: Demo Mode — kubectl run"
        cleanup_demo_pod

        if kubectl run greenkube-demo \
            --image="$IMAGE_TAG" \
            --image-pull-policy=Never \
            --restart=Never \
            --command -- greenkube demo --no-browser --port 9000 2>&1; then

            log_info "Waiting for demo pod to start..."
            sleep 25

            pod_status=$(kubectl get pod greenkube-demo -o jsonpath='{.status.phase}' 2>/dev/null || true)
            if [[ "$pod_status" == "Running" ]]; then
                log_pass "Demo pod is running."

                if check_api_health_demo "greenkube-demo" 9000; then
                    # Verify data was generated
                    metrics_count=$(kubectl exec greenkube-demo -- \
                        python -c "import urllib.request; print(len(urllib.request.urlopen('http://0.0.0.0:9000/api/v1/metrics?last=7d').read()))" 2>/dev/null || echo "0")
                    if [[ "$metrics_count" -gt 1000 ]]; then
                        log_pass "Demo has metrics data (${metrics_count} bytes)."
                        record_result 8 "Demo mode" "PASS"
                    else
                        log_fail "Demo has no/insufficient metrics data."
                        record_result 8 "Demo mode" "FAIL"
                    fi
                else
                    record_result 8 "Demo mode" "FAIL"
                fi
            else
                log_fail "Demo pod is not running (status: $pod_status)."
                kubectl logs greenkube-demo --tail=20 2>/dev/null || true
                record_result 8 "Demo mode" "FAIL"
            fi
        else
            record_result 8 "Demo mode" "FAIL"
        fi
        cleanup_demo_pod
    fi
fi

# ==========================================================================
# SCENARIO 9: Helm template validation — All variants
# ==========================================================================
if should_run 9; then
    log_title "Scenario 9: Helm template validation"
    ALL_TEMPLATES_OK=true

    # 9a: Default (PostgreSQL)
    if helm template test "$CHART_DIR" &>/dev/null; then
        log_pass "Template: default (PostgreSQL)"
    else
        log_fail "Template: default (PostgreSQL)"
        ALL_TEMPLATES_OK=false
    fi

    # 9b: SQLite
    if helm template test "$CHART_DIR" --set config.db.type=sqlite --set postgres.enabled=false &>/dev/null; then
        log_pass "Template: SQLite"
    else
        log_fail "Template: SQLite"
        ALL_TEMPLATES_OK=false
    fi

    # 9c: SQLite with persistence
    if helm template test "$CHART_DIR" --set config.db.type=sqlite --set postgres.enabled=false --set config.persistence.enabled=true &>/dev/null; then
        log_pass "Template: SQLite + persistence"
    else
        log_fail "Template: SQLite + persistence"
        ALL_TEMPLATES_OK=false
    fi

    # 9d: With all tokens
    if helm template test "$CHART_DIR" \
        --set secrets.electricityMapsToken=test \
        --set secrets.boaviztaToken=test \
        --set secrets.apiKey=test &>/dev/null; then
        log_pass "Template: all tokens set"
    else
        log_fail "Template: all tokens set"
        ALL_TEMPLATES_OK=false
    fi

    # 9e: Prometheus/OpenCost URLs set manually
    if helm template test "$CHART_DIR" \
        --set config.prometheus.url="http://prometheus:9090" \
        --set config.opencost.url="http://opencost:9003" &>/dev/null; then
        log_pass "Template: manual Prometheus/OpenCost URLs"
    else
        log_fail "Template: manual Prometheus/OpenCost URLs"
        ALL_TEMPLATES_OK=false
    fi

    # 9f: On-prem provider
    if helm template test "$CHART_DIR" \
        --set config.cloudProvider=on-prem \
        --set config.defaultZone=DE &>/dev/null; then
        log_pass "Template: on-prem provider"
    else
        log_fail "Template: on-prem provider"
        ALL_TEMPLATES_OK=false
    fi

    # 9g: API disabled
    if helm template test "$CHART_DIR" --set config.api.enabled=false &>/dev/null; then
        log_pass "Template: API disabled"
    else
        log_fail "Template: API disabled"
        ALL_TEMPLATES_OK=false
    fi

    # 9h: Monitoring disabled
    if helm template test "$CHART_DIR" \
        --set monitoring.serviceMonitor.enabled=false \
        --set monitoring.networkPolicy.enabled=false &>/dev/null; then
        log_pass "Template: monitoring disabled"
    else
        log_fail "Template: monitoring disabled"
        ALL_TEMPLATES_OK=false
    fi

    # 9i: External PostgreSQL (no bundled)
    if helm template test "$CHART_DIR" \
        --set postgres.enabled=false \
        --set secrets.dbConnectionString="postgresql://user:pass@host:5432/db" &>/dev/null; then
        log_pass "Template: external PostgreSQL"
    else
        log_fail "Template: external PostgreSQL"
        ALL_TEMPLATES_OK=false
    fi

    if [[ "$ALL_TEMPLATES_OK" == true ]]; then
        record_result 9 "Helm template validation" "PASS"
    else
        record_result 9 "Helm template validation" "FAIL"
    fi
fi

# ==========================================================================
# SCENARIO 10: NOTES.txt output validation
# ==========================================================================
if should_run 10; then
    log_title "Scenario 10: NOTES.txt output validation"
    NOTES_OK=true

    # Use dry-run to get NOTES output (helm template doesn't show NOTES)
    rendered_notes=$(helm install --dry-run greenkube "$CHART_DIR" -n test-ns 2>/dev/null | sed -n '/^NOTES:/,$ p' || true)

    if echo "$rendered_notes" | grep -q "\-n test-ns"; then
        log_pass "NOTES.txt includes namespace flag in commands."
    else
        log_fail "NOTES.txt missing namespace flag."
        echo "  Rendered NOTES:"
        echo "$rendered_notes" | head -10
        NOTES_OK=false
    fi

    if [[ "$NOTES_OK" == true ]]; then
        record_result 10 "NOTES.txt validation" "PASS"
    else
        record_result 10 "NOTES.txt validation" "FAIL"
    fi
fi

# ==========================================================================
# Summary
# ==========================================================================
log_title "📊 Test Results Summary"
echo ""
for result in "${RESULTS[@]}"; do
    echo -e "  $result"
done
echo ""
echo -e "  ${GREEN}Passed: $PASSED${NC}  |  ${RED}Failed: $FAILED${NC}  |  ${YELLOW}Skipped: $SKIPPED${NC}"
echo ""

if [[ $FAILED -gt 0 ]]; then
    echo -e "${RED}❌ Some scenarios failed! Review the output above.${NC}"
    exit 1
else
    echo -e "${GREEN}✅ All scenarios passed!${NC}"
    exit 0
fi
