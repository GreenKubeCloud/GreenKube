# Specification: "Apply Recommendation" Feature

**Version:** 1.0  
**Date:** February 23, 2026  
**Status:** Draft  
**Author:** GreenKube Team  

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Motivation & Goals](#2-motivation--goals)
3. [Security Architecture Decision](#3-security-architecture-decision)
4. [Scope & Boundaries](#4-scope--boundaries)
5. [Domain Model Changes](#5-domain-model-changes)
6. [API Specification](#6-api-specification)
7. [Core Layer: Action Engine](#7-core-layer-action-engine)
8. [Kubernetes Executor (Infrastructure Adapter)](#8-kubernetes-executor-infrastructure-adapter)
9. [RBAC & Helm Chart Changes](#9-rbac--helm-chart-changes)
10. [Audit Log](#10-audit-log)
11. [Frontend Integration](#11-frontend-integration)
12. [Configuration](#12-configuration)
13. [Testing Strategy](#13-testing-strategy)
14. [Rollout Strategy](#14-rollout-strategy)
15. [Future Evolution (SSO, Multi-Tenancy)](#15-future-evolution-sso-multi-tenancy)
16. [File Inventory & Task Checklist](#16-file-inventory--task-checklist)

---

## 1. Executive Summary

Today, GreenKube generates **read-only optimization recommendations** (zombie pod deletion, CPU/memory rightsizing, etc.). Users must manually run `kubectl` commands to act on them. This specification adds **"Apply" buttons** to the dashboard so that an authorized operator can apply a recommendation in a single click.

Because these actions **mutate live cluster resources** (patch Deployments, delete Pods, scale replicas), the feature must be designed with strong safety guardrails. After careful analysis, we chose a **server-side API-key gate + opt-in Helm flag** approach rather than a full-blown authentication/SSO system. The rationale is detailed in §3.

The design strictly follows GreenKube's existing **hexagonal architecture**: a new core-layer *ActionEngine* defines the business rules, a new *KubernetesExecutor* adapter performs the actual `kubectl`-equivalent calls, and the API layer exposes a thin REST endpoint. No infrastructure concern leaks into the core.

---

## 2. Motivation & Goals

| # | Goal | Description |
|---|------|-------------|
| G1 | **One-click apply** | Users can apply a recommendation from the dashboard without leaving the browser. |
| G2 | **Safety by default** | The feature is **disabled** by default. It must be explicitly opted-in via Helm values. |
| G3 | **Least-privilege RBAC** | GreenKube's service account receives write permissions **only** when the feature is enabled; otherwise it stays read-only (current behavior). |
| G4 | **Auditability** | Every applied action is recorded with timestamp, actor identifier, target resource, and outcome. |
| G5 | **Dry-run first** | Before applying, the system performs a Kubernetes dry-run to validate the patch and returns a preview to the user. |
| G6 | **Reversibility** | The system stores the *previous* resource spec so that a future "Undo" feature can be added. |
| G7 | **Hexagonal architecture** | Core logic (what to patch, validation rules) is infrastructure-agnostic. The K8s adapter is replaceable (e.g., for testing or multi-cloud). |
| G8 | **Modular for future auth** | The authorization check is behind an abstract `AuthorizationPort` interface, so that a future SSO/OIDC module can be plugged in without touching the core. |

---

## 3. Security Architecture Decision

### 3.1 Why Not Full Authentication/SSO Now?

We considered three options:

| Option | Pros | Cons |
|--------|------|------|
| **A. No auth, feature flag only** | Simplest; fast to ship | Anyone with network access to the API can mutate the cluster |
| **B. Static API key + feature flag** (chosen) | Simple; compatible with existing `kubectl port-forward` workflow; no user DB needed; key rotated via Helm Secret | Not user-granular; no per-namespace RBAC within GreenKube |
| **C. Full OIDC/SSO + RBAC** | Per-user audit, role-based access, enterprise-ready | Heavy; requires identity provider; blocks v1 shipping; overkill for a monitoring tool at this stage |

**Decision: Option B.**

Rationale:
- GreenKube is primarily a **monitoring & reporting** tool. Apply actions are an advanced convenience, not the core use case.
- Cluster administrators already control who can `port-forward` to the GreenKube service, providing a natural network-level gate.
- A static API key (stored in the existing Helm `Secret`) is sufficient for v1: it proves the caller is someone who was given the key by a cluster admin.
- The architecture is designed so that replacing the `StaticTokenAuthorizer` with an `OIDCAuthorizer` later requires **zero changes** to the core or the frontend contract — only a new adapter and a config flag.

### 3.2 Threat Model

| Threat | Mitigation |
|--------|-----------|
| Unauthorized user applies a destructive action | Feature disabled by default; API key required for all `/actions` endpoints; network access controlled by K8s RBAC (port-forward, Ingress auth) |
| API key leaked | Key stored in K8s Secret; rotatable via `helm upgrade`; audit log records all actions for forensics |
| GreenKube service account is overprivileged | Write RBAC rules are only created when `actions.enabled=true`; scoped to `apps/v1` Deployments/StatefulSets/ReplicaSets + core Pods; **no cluster-admin** |
| Accidental destructive action | Dry-run preview shown before apply; confirmation required in frontend; only the specific resource targeted by the recommendation is touched |
| Replay attacks | Each action request contains a unique recommendation fingerprint (hash of type + namespace + pod + timestamp); server rejects duplicates within a TTL |

### 3.3 Should GreenKube Users Map to Kubernetes Users?

**Not for v1.** GreenKube's service account performs actions on behalf of the operator. The audit log records the API-key identity (e.g., `"admin"` or a custom label attached to the key). In a future SSO iteration (§15), the authenticated user's identity would be forwarded as a Kubernetes *impersonation* header, delegating authorization to the cluster's own RBAC. This approach:
- Avoids GreenKube needing broad `impersonate` privileges today.
- Keeps the v1 implementation simple and self-contained.
- Provides a clear migration path to enterprise-grade security.

---

## 4. Scope & Boundaries

### 4.1 In Scope (v1)

| Recommendation Type | Action | K8s API Call |
|---------------------|--------|-------------|
| `RIGHTSIZING_CPU` | Patch container CPU request/limit | `PATCH /apis/apps/v1/namespaces/{ns}/deployments/{name}` |
| `RIGHTSIZING_MEMORY` | Patch container memory request/limit | Same as above |
| `ZOMBIE_POD` | Delete the pod (or scale owner to 0) | `DELETE /api/v1/namespaces/{ns}/pods/{name}` or `PATCH` scale to 0 |
| `OFF_PEAK_SCALING` | Scale the owner to 0 replicas | `PATCH` scale subresource |

### 4.2 Out of Scope (v1) — Future Iterations

| Item | Reason |
|------|--------|
| `AUTOSCALING_CANDIDATE` → create HPA | Complex; requires choosing target metrics & thresholds interactively |
| `CARBON_AWARE_SCHEDULING` → add node affinity | Requires understanding of node topology; better suited for a policy engine |
| `IDLE_NAMESPACE` → delete namespace | Too destructive for one-click; needs multi-step confirmation workflow |
| `OVERPROVISIONED_NODE` / `UNDERUTILIZED_NODE` | Node-level operations (cordon/drain) require orchestration beyond a single API call |
| Full SSO/OIDC authentication | Deferred to v2; architecture prepared (§15) |
| Undo / Rollback | Deferred; previous spec is stored in audit log for future implementation |

### 4.3 Supported Owner Kinds

The executor resolves the **owner** of a pod (via `owner_kind` / `owner_name` on the recommendation or the pod's `metadata.ownerReferences`) and patches the owner rather than the pod directly. Supported owners:

- `Deployment`
- `StatefulSet`
- `ReplicaSet` (resolves up to owning Deployment if possible)
- Bare `Pod` (direct delete only, no patching)

---

## 5. Domain Model Changes

### 5.1 New Enum: `ActionType`

```python
# src/greenkube/models/actions.py

class ActionType(str, Enum):
    """Types of cluster-mutating actions."""
    PATCH_CPU_REQUEST = "PATCH_CPU_REQUEST"
    PATCH_MEMORY_REQUEST = "PATCH_MEMORY_REQUEST"
    DELETE_POD = "DELETE_POD"
    SCALE_TO_ZERO = "SCALE_TO_ZERO"
```

### 5.2 New Enum: `ActionStatus`

```python
class ActionStatus(str, Enum):
    PENDING = "PENDING"         # Created, not yet executed
    DRY_RUN_OK = "DRY_RUN_OK" # Dry-run succeeded, awaiting confirmation
    DRY_RUN_FAILED = "DRY_RUN_FAILED"
    APPLIED = "APPLIED"         # Successfully applied to cluster
    FAILED = "FAILED"           # Apply attempt failed
    REJECTED = "REJECTED"       # Rejected by authorization or validation
```

### 5.3 New Model: `ActionRequest`

```python
class ActionRequest(BaseModel):
    """Inbound request to apply a recommendation."""
    recommendation_type: RecommendationType
    namespace: str
    pod_name: str
    owner_kind: Optional[str] = None
    owner_name: Optional[str] = None
    # For rightsizing:
    recommended_cpu_request_millicores: Optional[int] = None
    recommended_memory_request_bytes: Optional[int] = None
    # For scaling:
    target_replicas: Optional[int] = None  # 0 for scale-to-zero
    dry_run: bool = True  # Default to dry-run
```

### 5.4 New Model: `ActionResult`

```python
class ActionResult(BaseModel):
    """Result of an apply action."""
    action_id: str  # UUID
    action_type: ActionType
    status: ActionStatus
    namespace: str
    target_resource: str  # e.g., "deployment/nginx"
    message: str
    dry_run: bool
    previous_spec: Optional[dict] = None  # For undo
    applied_spec: Optional[dict] = None   # What was applied
    timestamp: datetime
    actor: str = "api-key"  # Identity of who triggered it
```

### 5.5 Extended `Recommendation` Model

Add one new field to the existing `Recommendation` model:

```python
# Added to existing Recommendation model in models/metrics.py
is_actionable: bool = Field(
    False,
    description="Whether this recommendation can be applied via the API."
)
```

The `Recommender` sets `is_actionable=True` for the 4 supported types (§4.1) and `False` for all others. The frontend uses this flag to conditionally show the "Apply" button.

---

## 6. API Specification

### 6.1 New Router: `src/greenkube/api/routers/actions.py`

All endpoints are behind the `/api/v1/actions` prefix and require the `X-GreenKube-Token` header when `actions.requireToken` is `true` (default).

#### `POST /api/v1/actions/preview`

**Purpose:** Validate and preview an action (Kubernetes dry-run) without applying it.

**Request Body:** `ActionRequest` with `dry_run=true` (enforced server-side).

**Response:** `ActionResult` with `status=DRY_RUN_OK` or `DRY_RUN_FAILED`.

**Status Codes:**
- `200` — Dry-run succeeded; response contains preview
- `400` — Invalid request (missing fields, unsupported type)
- `403` — Invalid or missing token
- `404` — Target resource not found in cluster
- `409` — Action already in progress for this resource
- `422` — Validation failed (e.g., recommended value is negative)
- `503` — Actions feature is disabled

#### `POST /api/v1/actions/apply`

**Purpose:** Apply a previously previewed action to the cluster.

**Request Body:** `ActionRequest` with `dry_run=false`.

**Response:** `ActionResult` with `status=APPLIED` or `FAILED`.

**Status Codes:** Same as preview, plus:
- `500` — Kubernetes API error during apply

#### `GET /api/v1/actions/history`

**Purpose:** Retrieve the audit log of applied actions.

**Query Params:**
- `namespace` (optional) — Filter by namespace
- `last` (optional, default `7d`) — Time window

**Response:** `List[ActionResult]`

#### `GET /api/v1/actions/status`

**Purpose:** Check if the actions feature is enabled and the service account has the required permissions.

**Response:**
```json
{
  "enabled": true,
  "has_write_permissions": true,
  "supported_actions": ["PATCH_CPU_REQUEST", "PATCH_MEMORY_REQUEST", "DELETE_POD", "SCALE_TO_ZERO"]
}
```

### 6.2 Authentication Dependency

```python
# src/greenkube/api/dependencies.py (additions)

async def verify_actions_enabled():
    """Raises 503 if actions feature is disabled."""
    if not config.ACTIONS_ENABLED:
        raise HTTPException(503, "Actions feature is disabled. Set actions.enabled=true in Helm values.")

async def verify_action_token(x_greenkube_token: str = Header(None)):
    """Validates the API key for action endpoints."""
    if not config.ACTIONS_REQUIRE_TOKEN:
        return "anonymous"
    if not x_greenkube_token or x_greenkube_token != config.ACTIONS_TOKEN:
        raise HTTPException(403, "Invalid or missing X-GreenKube-Token header.")
    return "api-key-holder"
```

### 6.3 Changes to Existing Recommendation Endpoint

The existing `GET /api/v1/recommendations` response gains the `is_actionable` field on each recommendation. This is backward-compatible (additive field with a default value).

---

## 7. Core Layer: Action Engine

### 7.1 Port Interface (Abstract)

```
src/greenkube/core/ports/
├── __init__.py
├── action_executor.py    # Abstract executor interface
└── authorizer.py         # Abstract authorization interface
```

#### `ActionExecutorPort` (Output Port)

```python
# src/greenkube/core/ports/action_executor.py

class ActionExecutorPort(ABC):
    """Port for executing cluster-mutating actions. Infrastructure-agnostic."""

    @abstractmethod
    async def patch_container_resources(
        self, namespace: str, owner_kind: str, owner_name: str,
        container_name: str, cpu_request_m: Optional[int], memory_request_bytes: Optional[int],
        dry_run: bool = True,
    ) -> ActionResult: ...

    @abstractmethod
    async def delete_pod(self, namespace: str, pod_name: str, dry_run: bool = True) -> ActionResult: ...

    @abstractmethod
    async def scale_replicas(
        self, namespace: str, owner_kind: str, owner_name: str,
        replicas: int, dry_run: bool = True,
    ) -> ActionResult: ...

    @abstractmethod
    async def check_write_permissions(self) -> bool: ...
```

#### `AuthorizerPort` (Input Port)

```python
# src/greenkube/core/ports/authorizer.py

class AuthorizerPort(ABC):
    """Port for authorization checks. Replaceable by SSO adapter later."""

    @abstractmethod
    async def authorize(self, token: Optional[str], action: ActionRequest) -> AuthorizationResult: ...
```

### 7.2 ActionEngine (Use Case)

```python
# src/greenkube/core/action_engine.py
```

The `ActionEngine` orchestrates the flow:

1. **Validate** the `ActionRequest` (required fields, supported types, value ranges)
2. **Authorize** via the `AuthorizerPort`
3. **Resolve** the target owner (if `owner_kind`/`owner_name` not provided, look them up)
4. **Map** `RecommendationType` → `ActionType` → executor method
5. **Execute** via the `ActionExecutorPort` (dry-run or real)
6. **Record** the result in the audit log (`ActionAuditRepository`)
7. **Return** `ActionResult`

**Mapping table (core logic, no K8s imports):**

| RecommendationType | ActionType | Executor Method |
|---------------------|-----------|-----------------|
| `RIGHTSIZING_CPU` | `PATCH_CPU_REQUEST` | `patch_container_resources(cpu_request_m=...)` |
| `RIGHTSIZING_MEMORY` | `PATCH_MEMORY_REQUEST` | `patch_container_resources(memory_request_bytes=...)` |
| `ZOMBIE_POD` | `DELETE_POD` or `SCALE_TO_ZERO` | `delete_pod()` if bare pod; `scale_replicas(0)` if owned |
| `OFF_PEAK_SCALING` | `SCALE_TO_ZERO` | `scale_replicas(0)` |

**Validation rules (in core, not in adapter):**
- `recommended_cpu_request_millicores` must be > 0 and ≤ 64000 (64 cores)
- `recommended_memory_request_bytes` must be > 0 and ≤ 128 GiB
- `target_replicas` must be ≥ 0
- `namespace` must not be a system namespace unless `actions.allowSystemNamespaces` is true
- `pod_name` must match `[a-z0-9]([-a-z0-9]*[a-z0-9])?` pattern

---

## 8. Kubernetes Executor (Infrastructure Adapter)

### 8.1 Implementation

```
src/greenkube/executors/
├── __init__.py
└── kubernetes_executor.py
```

```python
# src/greenkube/executors/kubernetes_executor.py

class KubernetesExecutor(ActionExecutorPort):
    """Implements cluster mutations via kubernetes_asyncio."""
```

This adapter:
- Uses the existing `ensure_k8s_config()` from `core/k8s_client.py`
- Gets `AppsV1Api` and `CoreV1Api` clients
- Implements each method with proper K8s API calls
- Captures the **previous spec** (current state before patching) for audit/undo
- Passes `dry_run=["All"]` for preview mode
- Handles K8s API errors gracefully (404, 403, 409, 500) and maps them to `ActionResult` statuses

### 8.2 Owner Resolution

When a recommendation targets a pod but the executor needs to patch the owner:

1. Read the pod via `CoreV1Api.read_namespaced_pod()`
2. Walk `metadata.ownerReferences` to find the controller
3. If owner is `ReplicaSet`, walk up to the owning `Deployment`
4. Patch the resolved owner's `spec.template.spec.containers[*].resources`

This logic lives in a helper `_resolve_owner()` within the executor.

---

## 9. RBAC & Helm Chart Changes

### 9.1 Conditional Write ClusterRole

A **new** ClusterRole is created **only** when `actions.enabled=true`:

```yaml
# helm-chart/templates/clusterrole-actions.yaml
{{- if .Values.actions.enabled }}
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ include "greenkube.fullname" . }}-actions
rules:
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "replicasets"]
    verbs: ["get", "list", "patch", "update"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale", "statefulsets/scale", "replicasets/scale"]
    verbs: ["get", "patch", "update"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "delete"]
{{- end }}
```

A corresponding `ClusterRoleBinding` binds this to the GreenKube service account.

**Key point:** The existing read-only `ClusterRole` is **unchanged**. Users who don't enable actions see zero RBAC change.

### 9.2 New Helm Values

```yaml
# values.yaml additions
actions:
  # Master switch — disabled by default
  enabled: false
  # Require API key for action endpoints
  requireToken: true
  # Allow actions on system namespaces (kube-system, etc.)
  allowSystemNamespaces: false
  # Namespace allowlist (empty = all non-system namespaces allowed)
  # Example: ["production", "staging"]
  allowedNamespaces: []
  # Namespace denylist (takes precedence over allowlist)
  denyNamespaces: ["kube-system", "kube-public", "kube-node-lease"]

# In the secrets section:
secrets:
  # API key for action endpoints (required if actions.requireToken is true)
  actionsToken: ""
```

### 9.3 ConfigMap Additions

```yaml
ACTIONS_ENABLED: "{{ .Values.actions.enabled }}"
ACTIONS_REQUIRE_TOKEN: "{{ .Values.actions.requireToken }}"
ACTIONS_ALLOW_SYSTEM_NAMESPACES: "{{ .Values.actions.allowSystemNamespaces }}"
ACTIONS_ALLOWED_NAMESPACES: "{{ .Values.actions.allowedNamespaces | join "," }}"
ACTIONS_DENY_NAMESPACES: "{{ .Values.actions.denyNamespaces | join "," }}"
```

### 9.4 Secret Additions

```yaml
ACTIONS_TOKEN: "{{ .Values.secrets.actionsToken | b64enc }}"
```

---

## 10. Audit Log

### 10.1 Storage

Actions are stored in a new `action_audit` table (PostgreSQL/SQLite) or `action_audit` index (Elasticsearch).

**Schema:**

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `action_type` | VARCHAR | `PATCH_CPU_REQUEST`, `DELETE_POD`, etc. |
| `status` | VARCHAR | `APPLIED`, `FAILED`, etc. |
| `namespace` | VARCHAR | Target namespace |
| `target_resource` | VARCHAR | `deployment/nginx`, `pod/worker-abc` |
| `recommendation_type` | VARCHAR | Original recommendation type |
| `actor` | VARCHAR | `"api-key-holder"` or future user identity |
| `message` | TEXT | Success/error message |
| `previous_spec` | JSONB | The resource spec before the change |
| `applied_spec` | JSONB | The patch that was applied |
| `dry_run` | BOOLEAN | Whether this was a dry-run |
| `created_at` | TIMESTAMPTZ | When the action was executed |

### 10.2 Repository

```
src/greenkube/storage/
├── action_audit_repository.py        # Abstract base
├── postgres_action_audit_repository.py
├── sqlite_action_audit_repository.py
```

Follows the exact same pattern as existing repositories (abstract base class, concrete implementations, factory instantiation).

---

## 11. Frontend Integration

### 11.1 UX Flow

```
 ┌──────────────────────────────────────────────┐
 │  Recommendation Card (existing)               │
 │                                                │
 │  💀 zombie-pod  ·  ZOMBIE_POD                 │
 │  Namespace: default                            │
 │  Reason: Pod cost $0.05 but consumed 100J     │
 │                                                │
 │  ┌────────────────┐  ┌─────────────────────┐  │
 │  │  🔍 Preview    │  │  ⚡ Apply           │  │
 │  └────────────────┘  └─────────────────────┘  │
 │                                                │
 └──────────────────────────────────────────────┘
           │                       │
           ▼                       ▼
 ┌─────────────────────┐  ┌───────────────────────┐
 │  Preview Modal       │  │  Confirm Modal         │
 │                      │  │                        │
 │  Action: Delete Pod  │  │  ⚠️ This will delete   │
 │  Target: pod/zombie  │  │  pod "zombie-pod" in   │
 │  Dry-run: ✅ OK      │  │  namespace "default".  │
 │                      │  │                        │
 │  Previous spec:      │  │  [Cancel]  [Confirm]   │
 │  { ... }             │  │                        │
 │                      │  └───────────────────────┘
 │  [Cancel] [Apply →]  │              │
 └─────────────────────┘              ▼
                            ┌───────────────────┐
                            │  Result Toast      │
                            │  ✅ Applied         │
                            │  or ❌ Failed       │
                            └───────────────────┘
```

### 11.2 Components

#### New: `ApplyButton.svelte`

A self-contained component receiving a `recommendation` prop. It:
1. Checks `recommendation.is_actionable` — if `false`, renders nothing.
2. Checks the actions feature status (cached from `GET /api/v1/actions/status`) — if disabled, renders a disabled tooltip: "Actions are disabled by the administrator."
3. On click: opens the Preview Modal → calls `POST /api/v1/actions/preview` → displays the result.
4. On confirm: calls `POST /api/v1/actions/apply` → shows success/error toast.

#### New: `ActionPreviewModal.svelte`

Displays:
- Action type with icon and human-readable label
- Target resource (`deployment/nginx` in namespace `production`)
- The diff: current spec vs. proposed spec (formatted as a side-by-side or unified diff)
- Warnings for destructive actions (delete, scale-to-zero) with red styling
- "Cancel" and "Apply" buttons

#### New: `ConfirmDialog.svelte`

A reusable confirmation dialog component with:
- A warning message
- Type-to-confirm for destructive actions (user types the pod name to confirm deletion)
- "Cancel" and "Confirm" buttons

#### Modified: `+page.svelte` (recommendations)

- Import `ApplyButton`
- Render `<ApplyButton {rec} />` inside each recommendation card, next to the priority badge
- Add a settings gear icon in the header that opens a panel to enter/store the API key (stored in `localStorage`, sent as `X-GreenKube-Token` header)
- Add "Action History" tab alongside the type filter tabs

### 11.3 API Client Additions

```javascript
// frontend/src/lib/api.js (additions)

/** POST with JSON body and optional auth token */
async function postRequest(path, body = {}, token = null) {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['X-GreenKube-Token'] = token;
    const res = await fetch(new URL(path, window.location.origin).toString(), {
        method: 'POST', headers, body: JSON.stringify(body),
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `API error ${res.status}`);
    }
    return res.json();
}

export function getActionsStatus() {
    return request(`${BASE}/actions/status`);
}

export function previewAction(actionRequest, token) {
    return postRequest(`${BASE}/actions/preview`, actionRequest, token);
}

export function applyAction(actionRequest, token) {
    return postRequest(`${BASE}/actions/apply`, { ...actionRequest, dry_run: false }, token);
}

export function getActionHistory({ namespace, last } = {}) {
    return request(`${BASE}/actions/history`, { namespace, last });
}
```

### 11.4 Svelte Store Additions

```javascript
// frontend/src/lib/stores.js (additions)

/** Persisted API key for actions */
export const actionsToken = writable(
    typeof localStorage !== 'undefined' ? localStorage.getItem('greenkube_actions_token') || '' : ''
);

/** Actions feature status (cached) */
export const actionsStatus = writable({ enabled: false, has_write_permissions: false, supported_actions: [] });
```

### 11.5 Styling & UX Guidelines

- **Apply buttons** use a distinct color from the existing UI (amber/orange) to signal a mutating action.
- **Destructive actions** (delete, scale-to-zero) use red buttons with an additional confirmation step.
- **Rightsizing patches** use green buttons (constructive action with savings).
- **Disabled states** are clearly communicated: grey button with tooltip explaining why (feature disabled, not actionable, missing token).
- **Loading states** show a spinner inside the button during preview/apply.
- **Success/error toasts** appear in the top-right corner with auto-dismiss after 5 seconds.
- All buttons follow the existing Tailwind design system (`btn-primary`, `btn-secondary` patterns from `app.css`).

---

## 12. Configuration

### 12.1 New Config Variables

```python
# src/greenkube/core/config.py (additions)

# --- Actions feature ---
ACTIONS_ENABLED = os.getenv("ACTIONS_ENABLED", "false").lower() in ("true", "1")
ACTIONS_REQUIRE_TOKEN = os.getenv("ACTIONS_REQUIRE_TOKEN", "true").lower() in ("true", "1")
ACTIONS_TOKEN = Config._get_secret("ACTIONS_TOKEN")
ACTIONS_ALLOW_SYSTEM_NAMESPACES = os.getenv("ACTIONS_ALLOW_SYSTEM_NAMESPACES", "false").lower() in ("true", "1")
ACTIONS_ALLOWED_NAMESPACES = [
    ns.strip() for ns in os.getenv("ACTIONS_ALLOWED_NAMESPACES", "").split(",") if ns.strip()
]
ACTIONS_DENY_NAMESPACES = [
    ns.strip() for ns in os.getenv(
        "ACTIONS_DENY_NAMESPACES", "kube-system,kube-public,kube-node-lease"
    ).split(",") if ns.strip()
]
```

### 12.2 Namespace Authorization Logic (Core)

```python
def is_namespace_allowed(namespace: str) -> bool:
    """Determines if an action is allowed in the given namespace."""
    if namespace in config.ACTIONS_DENY_NAMESPACES:
        return False
    if not config.ACTIONS_ALLOW_SYSTEM_NAMESPACES and namespace in SYSTEM_NAMESPACES:
        return False
    if config.ACTIONS_ALLOWED_NAMESPACES:
        return namespace in config.ACTIONS_ALLOWED_NAMESPACES
    return True
```

---

## 13. Testing Strategy

### 13.1 Unit Tests — Core Layer

**File:** `tests/core/test_action_engine.py`

| # | Test | Description |
|---|------|-------------|
| 1 | `test_validate_rightsizing_cpu_request` | Valid CPU rightsizing request passes validation |
| 2 | `test_validate_rightsizing_cpu_negative` | Negative CPU value → rejected |
| 3 | `test_validate_rightsizing_cpu_too_high` | > 64000m → rejected |
| 4 | `test_validate_rightsizing_memory_request` | Valid memory request passes |
| 5 | `test_validate_zombie_delete` | Valid zombie delete request passes |
| 6 | `test_validate_unsupported_type` | `AUTOSCALING_CANDIDATE` → rejected (not actionable) |
| 7 | `test_validate_system_namespace_blocked` | `kube-system` → rejected when system NS disabled |
| 8 | `test_validate_system_namespace_allowed` | `kube-system` → passes when system NS enabled |
| 9 | `test_validate_denied_namespace` | Namespace in deny list → rejected |
| 10 | `test_validate_allowed_namespace` | Namespace in allow list → passes |
| 11 | `test_map_rightsizing_cpu_to_action` | Maps `RIGHTSIZING_CPU` → `PATCH_CPU_REQUEST` |
| 12 | `test_map_zombie_bare_pod_to_delete` | `ZOMBIE_POD` bare pod → `DELETE_POD` |
| 13 | `test_map_zombie_owned_pod_to_scale` | `ZOMBIE_POD` owned pod → `SCALE_TO_ZERO` |
| 14 | `test_map_off_peak_to_scale_zero` | `OFF_PEAK_SCALING` → `SCALE_TO_ZERO` |
| 15 | `test_engine_dry_run_calls_executor` | Dry-run flows through to executor with `dry_run=True` |
| 16 | `test_engine_apply_calls_executor` | Apply flows through to executor with `dry_run=False` |
| 17 | `test_engine_records_audit_log` | Successful apply writes to audit repository |
| 18 | `test_engine_authorization_rejected` | Invalid token → `REJECTED` status |
| 19 | `test_engine_executor_failure` | Executor raises → `FAILED` status, error recorded |
| 20 | `test_is_namespace_allowed_deny_list` | Deny list takes precedence |
| 21 | `test_is_namespace_allowed_allow_list` | Allow list is respected |

### 13.2 Unit Tests — Kubernetes Executor

**File:** `tests/executors/test_kubernetes_executor.py`

| # | Test | Description |
|---|------|-------------|
| 1 | `test_patch_cpu_request_dry_run` | Calls K8s API with `dry_run=["All"]`, returns previous spec |
| 2 | `test_patch_cpu_request_apply` | Calls K8s API without dry_run, returns `APPLIED` |
| 3 | `test_patch_memory_request` | Patches memory on the correct container |
| 4 | `test_delete_pod` | Deletes pod, returns `APPLIED` |
| 5 | `test_scale_to_zero` | Patches scale subresource to 0 |
| 6 | `test_resolve_owner_deployment` | Pod → ReplicaSet → Deployment resolution |
| 7 | `test_resolve_owner_statefulset` | Pod → StatefulSet resolution |
| 8 | `test_resolve_owner_bare_pod` | Pod with no owner → returns pod itself |
| 9 | `test_resource_not_found_404` | K8s 404 → `FAILED` with clear message |
| 10 | `test_permission_denied_403` | K8s 403 → `FAILED` with RBAC hint |
| 11 | `test_check_write_permissions` | SelfSubjectAccessReview returns true/false |

### 13.3 API Tests — Actions Router

**File:** `tests/api/test_actions.py`

| # | Test | Description |
|---|------|-------------|
| 1 | `test_actions_disabled_returns_503` | Feature disabled → 503 |
| 2 | `test_preview_without_token_returns_403` | Missing token → 403 |
| 3 | `test_preview_with_valid_token_returns_200` | Valid token + request → 200 |
| 4 | `test_preview_invalid_request_returns_422` | Missing required fields → 422 |
| 5 | `test_apply_without_token_returns_403` | Missing token → 403 |
| 6 | `test_apply_with_valid_token_returns_200` | Valid apply → 200 |
| 7 | `test_apply_unsupported_type_returns_400` | Unsupported recommendation type → 400 |
| 8 | `test_actions_status_returns_feature_info` | `/actions/status` returns correct status |
| 9 | `test_actions_history_returns_list` | `/actions/history` returns audit entries |
| 10 | `test_actions_history_filter_by_namespace` | Namespace filter works |
| 11 | `test_token_not_required_when_disabled` | `requireToken=false` → no 403 |
| 12 | `test_system_namespace_blocked` | `kube-system` → 403 when not allowed |

### 13.4 Integration Tests

**File:** `tests/integration/test_actions_integration.py`

Using a mock K8s API (or kind cluster in CI):
1. Full flow: preview → apply → verify audit log
2. Rightsizing: apply CPU patch → verify Deployment spec changed
3. Zombie: delete pod → verify pod gone
4. Concurrent requests: two applies on same resource → one succeeds, one gets 409

### 13.5 Frontend Tests (Manual Checklist for v1)

| # | Scenario |
|---|----------|
| 1 | Apply button visible only for actionable recommendations |
| 2 | Apply button disabled when feature is off (with tooltip) |
| 3 | Preview modal shows correct diff |
| 4 | Destructive action shows type-to-confirm dialog |
| 5 | Success toast appears after apply |
| 6 | Error toast appears on failure |
| 7 | Token input persists in localStorage |
| 8 | Action history tab loads correctly |

---

## 14. Rollout Strategy

### Phase 1: Foundation (Backend)
1. Create `models/actions.py` (ActionType, ActionStatus, ActionRequest, ActionResult)
2. Create `core/ports/action_executor.py` and `core/ports/authorizer.py`
3. Create `core/action_engine.py` with validation + mapping logic
4. Add `is_actionable` field to `Recommendation` model
5. Update `Recommender` to set `is_actionable` for supported types
6. Write all core unit tests (§13.1)

### Phase 2: Infrastructure Adapters
7. Create `executors/kubernetes_executor.py`
8. Create `storage/action_audit_repository.py` (abstract + Postgres + SQLite)
9. Update `core/factory.py` with new factory functions
10. Write executor unit tests (§13.2)

### Phase 3: API Layer
11. Add new config variables to `config.py`
12. Add authentication dependencies to `api/dependencies.py`
13. Create `api/routers/actions.py`
14. Register router in `api/app.py`
15. Write API tests (§13.3)

### Phase 4: Helm Chart
16. Add `actions` section to `values.yaml`
17. Create `clusterrole-actions.yaml` and `clusterrolebinding-actions.yaml`
18. Update `configmap.yaml` and `secret.yaml`

### Phase 5: Frontend
19. Add API client methods (`api.js`)
20. Add stores (`stores.js`)
21. Create `ApplyButton.svelte`, `ActionPreviewModal.svelte`, `ConfirmDialog.svelte`
22. Update recommendations `+page.svelte`
23. Build production frontend

### Phase 6: Documentation & Release
24. Update `docs/architecture.md`
25. Update `README.md`
26. Update `current_task.md`
27. Build Docker image, deploy to Minikube, validate

---

## 15. Future Evolution (SSO, Multi-Tenancy)

The architecture is explicitly designed to support these future enhancements **without refactoring the core**:

### 15.1 SSO / OIDC Authentication

When the time comes to add real user authentication:

1. **Create `OIDCAuthorizer`** implementing `AuthorizerPort`
   - Validates JWT tokens from an external IdP (Google, Okta, Keycloak, LDAP via Dex)
   - Extracts user identity, groups, and roles from token claims
   - No changes needed in `ActionEngine` — it just calls `self.authorizer.authorize()`

2. **Add a config flag:** `AUTH_MODE = "static-token" | "oidc"`
   - Factory instantiates the right authorizer based on config

3. **Frontend changes:**
   - Replace the API-key input with an OIDC login flow (redirect-based or popup)
   - Store the JWT in `sessionStorage` (not `localStorage` for security)
   - Send it as `Authorization: Bearer <token>` instead of `X-GreenKube-Token`

### 15.2 Role-Based Access Control (Within GreenKube)

Future roles could include:

| Role | Permissions |
|------|------------|
| `viewer` | Read-only access to dashboard, metrics, recommendations |
| `operator` | Can preview and apply actions in assigned namespaces |
| `admin` | Can apply actions in all namespaces, manage configuration |

These roles would be extracted from OIDC token claims (e.g., `greenkube_role` claim or group membership) and checked by the `OIDCAuthorizer`.

### 15.3 Kubernetes User Impersonation

For organizations that want GreenKube actions to be authorized by the **cluster's own RBAC** (not GreenKube's):

1. Add the `impersonate` verb to GreenKube's ClusterRole
2. The executor adds `Impersonate-User` and `Impersonate-Group` headers to K8s API calls
3. The cluster's RBAC decides whether the impersonated user can patch the target resource
4. This provides the strongest security model but requires the most configuration

### 15.4 Extension Points Summary

| Component | Interface | Current Implementation | Future Implementation |
|-----------|-----------|----------------------|----------------------|
| Authorization | `AuthorizerPort` | `StaticTokenAuthorizer` | `OIDCAuthorizer` |
| Execution | `ActionExecutorPort` | `KubernetesExecutor` | `MockExecutor` (tests), `MultiClusterExecutor` |
| Audit Storage | `ActionAuditRepository` | Postgres/SQLite | Elasticsearch, external SIEM |
| Identity | Config + header | Static API key | JWT from IdP |

---

## 16. File Inventory & Task Checklist

### New Files

| File | Layer | Purpose |
|------|-------|---------|
| `src/greenkube/models/actions.py` | Domain | ActionType, ActionStatus, ActionRequest, ActionResult |
| `src/greenkube/core/ports/__init__.py` | Core | Ports package |
| `src/greenkube/core/ports/action_executor.py` | Core | Abstract executor interface |
| `src/greenkube/core/ports/authorizer.py` | Core | Abstract authorizer interface |
| `src/greenkube/core/action_engine.py` | Core | Use case orchestrator |
| `src/greenkube/executors/__init__.py` | Infra | Executors package |
| `src/greenkube/executors/kubernetes_executor.py` | Infra | K8s adapter |
| `src/greenkube/executors/static_token_authorizer.py` | Infra | Static API key authorizer |
| `src/greenkube/storage/action_audit_repository.py` | Infra | Abstract audit repository |
| `src/greenkube/storage/postgres_action_audit_repository.py` | Infra | PostgreSQL audit repo |
| `src/greenkube/storage/sqlite_action_audit_repository.py` | Infra | SQLite audit repo |
| `src/greenkube/api/routers/actions.py` | API | Actions router |
| `frontend/src/lib/components/ApplyButton.svelte` | Frontend | Apply button component |
| `frontend/src/lib/components/ActionPreviewModal.svelte` | Frontend | Preview/diff modal |
| `frontend/src/lib/components/ConfirmDialog.svelte` | Frontend | Confirmation dialog |
| `helm-chart/templates/clusterrole-actions.yaml` | Helm | Conditional write RBAC |
| `helm-chart/templates/clusterrolebinding-actions.yaml` | Helm | Conditional RBAC binding |
| `tests/core/test_action_engine.py` | Tests | Core unit tests |
| `tests/executors/__init__.py` | Tests | Executors test package |
| `tests/executors/test_kubernetes_executor.py` | Tests | Executor unit tests |
| `tests/api/test_actions.py` | Tests | API endpoint tests |
| `tests/integration/test_actions_integration.py` | Tests | Integration tests |

### Modified Files

| File | Change |
|------|--------|
| `src/greenkube/models/metrics.py` | Add `is_actionable` field to `Recommendation` |
| `src/greenkube/core/recommender.py` | Set `is_actionable=True` for supported types |
| `src/greenkube/core/config.py` | Add `ACTIONS_*` config variables |
| `src/greenkube/core/factory.py` | Add factory functions for executor, authorizer, audit repo |
| `src/greenkube/api/app.py` | Register actions router |
| `src/greenkube/api/dependencies.py` | Add `verify_actions_enabled`, `verify_action_token` |
| `src/greenkube/api/schemas.py` | Add `ActionRequestSchema`, `ActionResultSchema`, `ActionsStatusResponse` |
| `frontend/src/lib/api.js` | Add `postRequest`, action API methods |
| `frontend/src/lib/stores.js` | Add `actionsToken`, `actionsStatus` |
| `frontend/src/routes/recommendations/+page.svelte` | Integrate `ApplyButton`, add history tab |
| `helm-chart/values.yaml` | Add `actions` section |
| `helm-chart/templates/configmap.yaml` | Add `ACTIONS_*` env vars |
| `helm-chart/templates/secret.yaml` | Add `ACTIONS_TOKEN` |
| `docs/architecture.md` | Document actions feature |
| `README.md` | Document actions feature |
| `current_task.md` | Track progress |

---

*End of specification.*
