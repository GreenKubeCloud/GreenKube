{{/*
Expand the name of the chart.
*/}}
{{- define "greenkube.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "greenkube.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "greenkube.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "greenkube.labels" -}}
helm.sh/chart: {{ include "greenkube.chart" . }}
{{ include "greenkube.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "greenkube.selectorLabels" -}}
app.kubernetes.io/name: {{ include "greenkube.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "greenkube.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "greenkube.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the application image reference.

Digest pinning uses image.digest (repo@sha256:...). Setting a digest in
image.tag is a common mistake that renders an invalid Kubernetes image
reference, so reject it with an actionable message.
*/}}
{{- define "greenkube.image" -}}
{{- $repository := required "image.repository is required" .Values.image.repository -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion | toString -}}
{{- $digest := .Values.image.digest | default "" | toString -}}
{{- if $digest -}}
  {{- if contains "@" $repository -}}
    {{- fail "image.repository must not contain a digest when image.digest is set; use image.repository=<repo> and image.digest=sha256:<digest>" -}}
  {{- end -}}
  {{- if not (regexMatch "^sha256:[0-9a-fA-F]{64}$" $digest) -}}
    {{- fail "image.digest must use the form sha256:<64 hexadecimal characters>" -}}
  {{- end -}}
  {{- printf "%s@%s" $repository $digest -}}
{{- else if hasPrefix "sha256:" $tag -}}
  {{- fail "image.tag must be a tag, not a digest; pin the image with image.digest=sha256:<64 hexadecimal characters>" -}}
{{- else if contains "@" $repository -}}
  {{- if and (hasSuffix "@sha256" $repository) (regexMatch "^[0-9a-fA-F]{64}$" $tag) -}}
    {{- printf "%s:%s" $repository $tag -}}
  {{- else if regexMatch "@sha256:[0-9a-fA-F]{64}$" $repository -}}
    {{- $repository -}}
  {{- else -}}
    {{- fail "image.repository contains an invalid digest reference; use image.repository=<repo> with image.digest" -}}
  {{- end -}}
{{- else -}}
  {{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end }}

{{/*
Return the name of the Secret to use.
If secrets.existingSecret is set, use that; otherwise use the Secret that holds
generated credentials. The credentials bootstrap hook creates that Secret once,
outside the Helm release, so the chart never renders generated values (N1).
*/}}
{{- define "greenkube.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "greenkube.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret holding the generated first-admin password (F14), or empty
when auth.admin.existingSecret provides it, sessions are off (api_key mode), or
RBAC is not created (nothing could generate or read it then). SSO modes (F16)
keep the local admin as a break-glass account.
*/}}
{{- define "greenkube.initialAdminSecretName" -}}
{{- $admin := (.Values.auth | default dict).admin | default dict -}}
{{- if and .Values.rbac.create (ne (.Values.config.apiAuthMode | default "session") "api_key") (not $admin.existingSecret) -}}
{{- printf "%s-initial-admin" (include "greenkube.fullname" . | trunc 49 | trimSuffix "-") -}}
{{- end -}}
{{- end -}}

{{/*
Name of the chart-rendered Secret that holds credentials supplied in values.
*/}}
{{- define "greenkube.valuesSecretName" -}}
{{- printf "%s-values" (include "greenkube.fullname" .) -}}
{{- end -}}

{{/*
True when the chart deploys and owns the PostgreSQL database.
*/}}
{{- define "greenkube.bundledPostgres" -}}
{{- if .Values.postgres.enabled -}}true{{- end -}}
{{- end -}}

{{/*
Credentials supplied through values, as a YAML map of key to plain value.
Rendering depends on values only (no lookup, no random function), so every
`helm template` render is identical (BUG-58). Empty with secrets.existingSecret.
A key set in values is always read from the values Secret, so a Settings-page
override of it lasts only until the next Pod restart. Keys left empty (every
default, including the service URLs) are read from greenkube.secretName, where
Settings-page overrides are stored, and otherwise fall back to the app default.
*/}}
{{- define "greenkube.secretValues" -}}
{{- $data := dict -}}
{{- if not .Values.secrets.existingSecret -}}
  {{- $secrets := .Values.secrets -}}
  {{- range $pair := list (list "ELECTRICITY_MAPS_TOKEN" $secrets.electricityMapsToken) (list "BOAVIZTA_TOKEN" $secrets.boaviztaToken) (list "WATTNET_EMAIL" $secrets.wattnetEmail) (list "WATTNET_PASSWORD" $secrets.wattnetPassword) (list "GREENKUBE_API_KEY" $secrets.apiKey) (list "GREENKUBE_METRICS_TOKEN" $secrets.metricsToken) (list "LEDGER_CHAIN_KEY" $secrets.ledgerChainKey) (list "PROMETHEUS_URL" $.Values.config.prometheus.url) (list "OPENCOST_API_URL" $.Values.config.opencost.url) (list "BOAVIZTA_API_URL" $.Values.config.boavizta.url) -}}
    {{- if index $pair 1 -}}
      {{- $_ := set $data (index $pair 0) (index $pair 1 | toString) -}}
    {{- end -}}
  {{- end -}}
  {{- if include "greenkube.bundledPostgres" . -}}
    {{- with .Values.postgres.auth.password -}}
      {{- $_ := set $data "POSTGRES_PASSWORD" (toString .) -}}
      {{- $_ := set $data "DB_CONNECTION_STRING" (printf "postgresql://%s:%s@%s-postgres:5432/%s" $.Values.postgres.auth.username (toString .) (include "greenkube.fullname" $) $.Values.postgres.auth.database) -}}
    {{- end -}}
  {{- else if .Values.secrets.dbConnectionString -}}
    {{- $_ := set $data "DB_CONNECTION_STRING" (toString .Values.secrets.dbConnectionString) -}}
  {{- end -}}
  {{- range $pair := list (list "PROMETHEUS_USERNAME" $secrets.prometheus.username) (list "PROMETHEUS_PASSWORD" $secrets.prometheus.password) (list "PROMETHEUS_BEARER_TOKEN" $secrets.prometheus.bearerToken) -}}
    {{- if index $pair 1 -}}
      {{- $_ := set $data (index $pair 0) (index $pair 1 | toString) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- toYaml $data -}}
{{- end -}}

{{/*
Name of the Secret that provides one application key:
secrets.existingSecret when set; otherwise the values Secret for a key supplied
in values, else the generated-credentials Secret.
Usage: include "greenkube.secretNameFor" (dict "context" $ "key" "DB_CONNECTION_STRING")
*/}}
{{- define "greenkube.secretNameFor" -}}
{{- $context := .context -}}
{{- if $context.Values.secrets.existingSecret -}}
{{- $context.Values.secrets.existingSecret -}}
{{- else if hasKey (include "greenkube.secretValues" $context | fromYaml) .key -}}
{{- include "greenkube.valuesSecretName" $context -}}
{{- else -}}
{{- include "greenkube.fullname" $context -}}
{{- end -}}
{{- end -}}

{{/*
True when the credentials bootstrap hook must run. Without RBAC the hook cannot
create the generated-credentials Secret, so generation then requires an
explicit Secret instead.
*/}}
{{- define "greenkube.credentialsBootstrapEnabled" -}}
{{- if not .Values.secrets.existingSecret -}}
  {{- $generates := or (include "greenkube.generateApiKey" .) (and (include "greenkube.bundledPostgres" .) (not .Values.postgres.auth.password)) -}}
  {{- if .Values.rbac.create -}}
true
  {{- else if $generates -}}
    {{- fail "rbac.create=false: the chart cannot create generated credentials without its bootstrap RBAC. Set secrets.existingSecret (recommended for GitOps), or supply secrets.apiKey and, for the bundled PostgreSQL, postgres.auth.password." -}}
  {{- end -}}
{{- else if or (include "greenkube.initialAdminSecretName" .) (eq (include "greenkube.scrapeTokenCopy" .) "hook") -}}
{{- /* secrets.existingSecret: the hook only creates the initial-admin Secret and copies the scrape token. */ -}}
true
{{- end -}}
{{- end -}}

{{/*
Name of the credentials bootstrap hook resources (Job, ServiceAccount, Role).
*/}}
{{- define "greenkube.credentialsBootstrapName" -}}
{{- printf "%s-credentials-bootstrap" (include "greenkube.fullname" . | trunc 41 | trimSuffix "-") -}}
{{- end -}}

{{- define "greenkube.configChecksum" -}}
{{- $safeConfig := deepCopy .Values.config -}}
{{- $_ := set $safeConfig "prometheus" (omit .Values.config.prometheus "url") -}}
{{- $_ := set $safeConfig "opencost" (omit .Values.config.opencost "url") -}}
{{- $config := dict "releaseName" .Release.Name "releaseNamespace" .Release.Namespace "config" $safeConfig "automation" .Values.automation "sources" .Values.sources "impact" .Values.impact "auth" .Values.auth "effective" (include "greenkube.values" . | fromYaml) "secretName" (include "greenkube.secretName" .) "kubeVersion" .Capabilities.KubeVersion.Version "apiVersions" .Capabilities.APIVersions -}}
{{- toJson $config | sha256sum -}}
{{- end -}}

{{/*
Project only the application Secret keys required by one component.
*/}}
{{- define "greenkube.applicationSecretItems" -}}
{{- $context := .context -}}
{{- $component := .component -}}
{{- $keys := list "DB_CONNECTION_STRING" -}}
{{- if or (eq $component "api") (eq $component "controller") -}}
  {{- range $key := list
      "ELECTRICITY_MAPS_TOKEN"
      "BOAVIZTA_TOKEN"
      "PROMETHEUS_URL"
      "PROMETHEUS_USERNAME"
      "PROMETHEUS_PASSWORD"
      "PROMETHEUS_BEARER_TOKEN"
      "OPENCOST_API_URL"
      "OPENCOST_USERNAME"
      "OPENCOST_PASSWORD"
      "OPENCOST_BEARER_TOKEN"
      "BOAVIZTA_API_URL"
      "WATTNET_EMAIL"
      "WATTNET_PASSWORD"
      "LEDGER_CHAIN_KEY"
  -}}
    {{- $keys = append $keys $key -}}
  {{- end -}}
{{- end -}}
{{- if or (eq $component "api") (eq $component "worker") -}}
  {{- $keys = append $keys "GREENKUBE_API_KEY" -}}
{{- end -}}
{{- /* The metrics:read scrape token checked by each component's metrics endpoint. */ -}}
{{- if and (has $component (list "api" "controller" "worker")) (include "greenkube.dedicatedScrapeToken" $context) -}}
  {{- $keys = append $keys "GREENKUBE_METRICS_TOKEN" -}}
{{- end -}}
{{- range $key := $keys }}
- key: {{ $key }}
  path: {{ $key }}
{{- end -}}
{{- end -}}

{{/*
Projected Secret sources for one component: each key comes from exactly one
Secret (see greenkube.secretNameFor), and a Secret without keys is omitted.
*/}}
{{- define "greenkube.applicationSecretSources" -}}
{{- $context := .context -}}
{{- $items := include "greenkube.applicationSecretItems" . | fromYamlArray -}}
{{- $names := list (include "greenkube.secretName" $context) (include "greenkube.valuesSecretName" $context) -}}
{{- range $name := $names | uniq }}
  {{- $selected := list -}}
  {{- range $item := $items -}}
    {{- if eq (include "greenkube.secretNameFor" (dict "context" $context "key" $item.key)) $name -}}
      {{- $selected = append $selected $item -}}
    {{- end -}}
  {{- end -}}
  {{- if $selected }}
- secret:
    name: {{ $name }}
    optional: true
    items:
      {{- toYaml $selected | nindent 6 }}
  {{- end -}}
{{- end -}}
{{- $admin := ($context.Values.auth | default dict).admin | default dict -}}
{{- if and $admin.existingSecret (eq .component "api") }}
- secret:
    name: {{ $admin.existingSecret }}
    items:
      - key: {{ $admin.existingSecretKey | default "password" }}
        path: AUTH_ADMIN_PASSWORD
{{- end -}}
{{- $oidc := ($context.Values.auth | default dict).oidc | default dict -}}
{{- if and $oidc.existingSecret (eq .component "api") (eq ($context.Values.config.apiAuthMode | default "session") "oidc") }}
- secret:
    name: {{ $oidc.existingSecret }}
    items:
      - key: {{ $oidc.existingSecretKey | default "clientSecret" }}
        path: OIDC_CLIENT_SECRET
{{- end -}}
{{- end -}}

{{- define "greenkube.applicationSecretVolumeMount" -}}
{{- $volumeName := .volumeName | default "secrets" -}}
- name: {{ $volumeName }}
  mountPath: /etc/greenkube/secrets
  readOnly: true
{{- end -}}

{{- define "greenkube.applicationSecretVolume" -}}
{{- $context := .context -}}
{{- $component := .component -}}
{{- $volumeName := .volumeName | default "secrets" -}}
- name: {{ $volumeName }}
  projected:
    sources:
      {{- include "greenkube.applicationSecretSources" (dict "context" $context "component" $component) | trim | nindent 6 }}
{{- end -}}

{{/*
Return the ServiceAccount of one component (api, controller, worker). With
serviceAccount.create=false every component uses serviceAccount.name.
*/}}
{{- define "greenkube.componentServiceAccountName" -}}
{{- if .context.Values.serviceAccount.create -}}
{{- printf "%s-%s" (include "greenkube.fullname" .context) .component | trunc 63 | trimSuffix "-" }}
{{- else -}}
{{- default "default" .context.Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Return the controller Lease name. The Lease is always rendered (F11).
*/}}
{{- define "greenkube.leaseName" -}}
{{- $v := include "greenkube.values" . | fromYaml -}}
{{- default (printf "%s-leader" (include "greenkube.fullname" .)) ($v.controller.leaderElection | default dict).leaseName | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Return the controller metrics/health port (0 disables the listener; the
values schema bounds it).
*/}}
{{- define "greenkube.controllerMetricsPort" -}}
{{- $v := include "greenkube.values" . | fromYaml -}}
{{- $v.controller.metricsPort | default 0 | int -}}
{{- end -}}

{{/*
Return the dedicated worker metrics port.
*/}}
{{- define "greenkube.workerMetricsPort" -}}
{{- $v := include "greenkube.values" . | fromYaml -}}
{{- $v.worker.metricsPort | default 8001 | int -}}
{{- end -}}

{{/*
Name of the renderer Deployment and Service.
*/}}
{{- define "greenkube.rendererName" -}}
{{- printf "%s-renderer" (include "greenkube.fullname" . | trunc 50 | trimSuffix "-") -}}
{{- end -}}

{{/*
Name of the ConfigMap holding non-sensitive settings. 0.3.x rendered
"<fullname>" as a Helm hook, which Helm cannot adopt into the release, so the
regular resource has its own name (an upgrade leaves the old hook ConfigMap
behind; docs/upgrading.md).
*/}}
{{- define "greenkube.configMapName" -}}
{{- printf "%s-config" (include "greenkube.fullname" . | trunc 56 | trimSuffix "-") -}}
{{- end -}}

{{/*
Probe timing fields shared by every probe.
*/}}
{{- define "greenkube.probeTimings" -}}
initialDelaySeconds: {{ .initialDelaySeconds | default 5 }}
periodSeconds: {{ .periodSeconds | default 10 }}
timeoutSeconds: {{ .timeoutSeconds | default 5 }}
failureThreshold: {{ .failureThreshold | default 3 }}
successThreshold: {{ .successThreshold | default 1 }}
{{- end -}}
