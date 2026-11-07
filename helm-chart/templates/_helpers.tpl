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
Name of the chart-rendered Secret that holds credentials supplied in values.
*/}}
{{- define "greenkube.valuesSecretName" -}}
{{- printf "%s-values" (include "greenkube.fullname" .) -}}
{{- end -}}

{{/*
True when the chart deploys and owns the PostgreSQL database.
*/}}
{{- define "greenkube.bundledPostgres" -}}
{{- if and .Values.postgres.enabled (eq .Values.config.db.type "postgres") -}}true{{- end -}}
{{- end -}}

{{/*
Credentials supplied through values, as a YAML map of key to plain value.
Rendering depends on values only (no lookup, no random function), so every
`helm template` render is identical (BUG-58). Empty with secrets.existingSecret.
*/}}
{{- define "greenkube.secretValues" -}}
{{- $data := dict -}}
{{- if not .Values.secrets.existingSecret -}}
  {{- $secrets := .Values.secrets -}}
  {{- range $pair := list (list "ELECTRICITY_MAPS_TOKEN" $secrets.electricityMapsToken) (list "BOAVIZTA_TOKEN" $secrets.boaviztaToken) (list "WATTNET_EMAIL" $secrets.wattnetEmail) (list "WATTNET_PASSWORD" $secrets.wattnetPassword) (list "GREENKUBE_API_KEY" $secrets.apiKey) (list "PROMETHEUS_URL" $.Values.config.prometheus.url) (list "OPENCOST_API_URL" $.Values.config.opencost.url) (list "BOAVIZTA_API_URL" $.Values.config.boavizta.url) -}}
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
  {{- $generates := or (not .Values.secrets.apiKey) (and (include "greenkube.bundledPostgres" .) (not .Values.postgres.auth.password)) -}}
  {{- if .Values.rbac.create -}}
true
  {{- else if $generates -}}
    {{- fail "rbac.create=false: the chart cannot create generated credentials without its bootstrap RBAC. Set secrets.existingSecret (recommended for GitOps), or supply secrets.apiKey and, for the bundled PostgreSQL, postgres.auth.password." -}}
  {{- end -}}
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
{{- $config := dict "releaseName" .Release.Name "releaseNamespace" .Release.Namespace "profile" .Values.profile "config" $safeConfig "automation" .Values.automation "rendererSandbox" .Values.rendererSandbox "secretName" (include "greenkube.secretName" .) "kubeVersion" .Capabilities.KubeVersion.Version "apiVersions" .Capabilities.APIVersions -}}
{{- toJson $config | sha256sum -}}
{{- end -}}

{{/*
Project only the application Secret keys required by one component.
*/}}
{{- define "greenkube.applicationSecretItems" -}}
{{- $context := .context -}}
{{- $component := .component -}}
{{- $keys := list -}}
{{- if eq $context.Values.config.db.type "postgres" -}}
  {{- $keys = append $keys "DB_CONNECTION_STRING" -}}
{{- end -}}
{{- if or (eq $component "api") (eq $component "controller") (eq $component "app") -}}
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
  -}}
    {{- $keys = append $keys $key -}}
  {{- end -}}
{{- end -}}
{{- if or (eq $component "api") (eq $component "automation") (eq $component "automation-worker") -}}
  {{- $keys = append $keys "GREENKUBE_API_KEY" -}}
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
Return the ServiceAccount used by a production component.
*/}}
{{- define "greenkube.componentServiceAccountName" -}}
{{- printf "%s-%s" (include "greenkube.fullname" .context) .component | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Return the production Lease name.
*/}}
{{- define "greenkube.leaseName" -}}
{{- default (printf "%s-leader" (include "greenkube.fullname" .)) .Values.production.leaderElection.leaseName | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/*
Return the configured automation mode, defaulting by deployment profile.
*/}}
{{- define "greenkube.automationMode" -}}
{{- $mode := .Values.automation.mode | default "" -}}
{{- if $mode -}}
{{- $mode -}}
{{- else if eq .Values.profile "production" -}}
worker
{{- else -}}
inline
{{- end -}}
{{- end -}}

{{/*
Validate and return the dedicated worker metrics port.
*/}}
{{- define "greenkube.workerMetricsPort" -}}
{{- $rawPort := .Values.automation.workerMetricsPort | toString -}}
{{- if not (regexMatch "^[0-9]+$" $rawPort) -}}
{{- fail "automation.workerMetricsPort must be an integer between 1 and 65535" -}}
{{- end -}}
{{- $port := int $rawPort -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail "automation.workerMetricsPort must be an integer between 1 and 65535" -}}
{{- end -}}
{{- $port -}}
{{- end -}}