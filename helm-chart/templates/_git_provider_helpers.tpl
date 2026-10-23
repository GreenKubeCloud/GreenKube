{{/*
Normalize the flat and documented nested provider Secret shapes.
*/}}
{{- define "greenkube.normalizedAutomationProviders" -}}
{{- $providers := .Values.automation.providers | default (list) -}}
{{- if not (kindIs "slice" $providers) -}}
{{- fail "automation.providers must be a list" -}}
{{- end -}}
{{- $normalizedProviders := list -}}
{{- range $index, $provider := $providers -}}
  {{- if not (kindIs "map" $provider) -}}
    {{- fail (printf "automation.providers[%d] must be a map" $index) -}}
  {{- end -}}
  {{- $normalized := deepCopy $provider -}}
  {{- if hasKey $provider "tokenSecret" -}}
    {{- $tokenSecret := get $provider "tokenSecret" -}}
    {{- if not (kindIs "map" $tokenSecret) -}}
      {{- fail (printf "automation.providers[%d].tokenSecret must be a map" $index) -}}
    {{- end -}}
    {{- range $pair := (list (list "name" "secretName") (list "key" "secretKey") (list "namespace" "secretNamespace")) -}}
      {{- $nestedKey := index $pair 0 -}}
      {{- $flatKey := index $pair 1 -}}
      {{- if hasKey $tokenSecret $nestedKey -}}
        {{- if and (hasKey $normalized $flatKey) (ne (get $normalized $flatKey) (get $tokenSecret $nestedKey)) -}}
          {{- fail (printf "automation.providers[%d] has conflicting %s and tokenSecret.%s values" $index $flatKey $nestedKey) -}}
        {{- end -}}
        {{- $_ := set $normalized $flatKey (get $tokenSecret $nestedKey) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- if hasKey $provider "repositories" -}}
    {{- if and (hasKey $normalized "repositoryAllowlist") (ne (toJson (get $normalized "repositoryAllowlist")) (toJson (get $provider "repositories"))) -}}
      {{- fail (printf "automation.providers[%d] has conflicting repositories and repositoryAllowlist values" $index) -}}
    {{- end -}}
    {{- $_ := set $normalized "repositoryAllowlist" (get $provider "repositories") -}}
  {{- end -}}
  {{- $normalizedProviders = append $normalizedProviders $normalized -}}
{{- end -}}
{{- toYaml $normalizedProviders -}}
{{- end -}}

{{/*
Validate configured Git providers and keep Secret references within the release namespace.
*/}}
{{- define "greenkube.validateAutomationProviders" -}}
{{- $providers := include "greenkube.normalizedAutomationProviders" . | fromYamlArray -}}
{{- $seenIDs := dict -}}
{{- $idPattern := "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" -}}
{{- $secretNamePattern := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" -}}
{{- $secretKeyPattern := "^[A-Za-z0-9._-]+$" -}}
{{- $namespacePattern := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" -}}
{{- range $index, $provider := $providers -}}
  {{- if not (kindIs "map" $provider) -}}
    {{- fail (printf "automation.providers[%d] must be a map" $index) -}}
  {{- end -}}
  {{- $id := get $provider "id" -}}
  {{- if or (not (kindIs "string" $id)) (gt (len $id) 128) (not (regexMatch $idPattern $id)) -}}
    {{- fail (printf "automation.providers[%d].id must be a valid provider ID" $index) -}}
  {{- end -}}
  {{- if hasKey $seenIDs $id -}}
    {{- fail "automation.providers IDs must be unique" -}}
  {{- end -}}
  {{- $_ := set $seenIDs $id true -}}

  {{- $providerType := get $provider "type" -}}
  {{- if not (kindIs "string" $providerType) -}}
    {{- fail (printf "automation.providers[%d].type must be github, gitlab, or gitea" $index) -}}
  {{- end -}}
  {{- if not (has (lower $providerType) (list "github" "gitlab" "gitea")) -}}
    {{- fail (printf "automation.providers[%d].type must be github, gitlab, or gitea" $index) -}}
  {{- end -}}

  {{- if and (hasKey $provider "apiBaseUrl") (not (kindIs "string" (get $provider "apiBaseUrl"))) -}}
    {{- fail (printf "automation.providers[%d].apiBaseUrl must be a string" $index) -}}
  {{- end -}}

  {{- $secretName := get $provider "secretName" -}}
  {{- if or (not (kindIs "string" $secretName)) (gt (len $secretName) 253) (not (regexMatch $secretNamePattern $secretName)) -}}
    {{- fail (printf "automation.providers[%d].secretName must be a valid Kubernetes Secret name" $index) -}}
  {{- end -}}
  {{- $secretKey := get $provider "secretKey" -}}
  {{- if or (not (kindIs "string" $secretKey)) (gt (len $secretKey) 253) (not (regexMatch $secretKeyPattern $secretKey)) (has $secretKey (list "." "..")) -}}
    {{- fail (printf "automation.providers[%d].secretKey must be a valid Kubernetes Secret key" $index) -}}
  {{- end -}}

  {{- if hasKey $provider "secretNamespace" -}}
    {{- $secretNamespace := get $provider "secretNamespace" -}}
    {{- if or (not (kindIs "string" $secretNamespace)) (gt (len $secretNamespace) 63) (not (regexMatch $namespacePattern $secretNamespace)) -}}
      {{- fail (printf "automation.providers[%d].secretNamespace must be a valid Kubernetes namespace" $index) -}}
    {{- end -}}
    {{- if ne $secretNamespace $.Release.Namespace -}}
      {{- fail (printf "automation.providers[%d].secretNamespace must match the Helm release namespace" $index) -}}
    {{- end -}}
  {{- end -}}

  {{- $hasWebhookSecretName := hasKey $provider "webhookSecretName" -}}
  {{- $hasWebhookSecretKey := hasKey $provider "webhookSecretKey" -}}
  {{- if ne $hasWebhookSecretName $hasWebhookSecretKey -}}
    {{- fail (printf "automation.providers[%d] must set both webhookSecretName and webhookSecretKey" $index) -}}
  {{- end -}}
  {{- if $hasWebhookSecretName -}}
    {{- $webhookSecretName := get $provider "webhookSecretName" -}}
    {{- if or (not (kindIs "string" $webhookSecretName)) (gt (len $webhookSecretName) 253) (not (regexMatch $secretNamePattern $webhookSecretName)) -}}
      {{- fail (printf "automation.providers[%d].webhookSecretName must be a valid Kubernetes Secret name" $index) -}}
    {{- end -}}
    {{- $webhookSecretKey := get $provider "webhookSecretKey" -}}
    {{- if or (not (kindIs "string" $webhookSecretKey)) (gt (len $webhookSecretKey) 253) (not (regexMatch $secretKeyPattern $webhookSecretKey)) (has $webhookSecretKey (list "." "..")) -}}
      {{- fail (printf "automation.providers[%d].webhookSecretKey must be a valid Kubernetes Secret key" $index) -}}
    {{- end -}}
  {{- end -}}
  {{- if and (hasKey $provider "webhookSecretNamespace") (ne (get $provider "webhookSecretNamespace") "") -}}
    {{- $webhookSecretNamespace := get $provider "webhookSecretNamespace" -}}
    {{- if or (not (kindIs "string" $webhookSecretNamespace)) (gt (len $webhookSecretNamespace) 63) (not (regexMatch $namespacePattern $webhookSecretNamespace)) -}}
      {{- fail (printf "automation.providers[%d].webhookSecretNamespace must be a valid Kubernetes namespace" $index) -}}
    {{- end -}}
    {{- if ne $webhookSecretNamespace $.Release.Namespace -}}
      {{- fail (printf "automation.providers[%d].webhookSecretNamespace must match the Helm release namespace" $index) -}}
    {{- end -}}
    {{- if not $hasWebhookSecretName -}}
      {{- fail (printf "automation.providers[%d].webhookSecretNamespace requires a webhook Secret reference" $index) -}}
    {{- end -}}
  {{- end -}}

  {{- $allowlist := get $provider "repositoryAllowlist" -}}
  {{- if or (not (kindIs "slice" $allowlist)) (eq (len $allowlist) 0) -}}
    {{- fail (printf "automation.providers[%d].repositoryAllowlist must contain at least one pattern" $index) -}}
  {{- end -}}
  {{- range $patternIndex, $pattern := $allowlist -}}
    {{- if or (not (kindIs "string" $pattern)) (eq (trim $pattern) "") (ne $pattern (trim $pattern)) (regexMatch "[[:space:][:cntrl:]]" $pattern) -}}
      {{- fail (printf "automation.providers[%d].repositoryAllowlist[%d] must be a non-empty string" $index $patternIndex) -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{/*
Serialize the runtime parser's provider schema with Secret references only.
*/}}
{{- define "greenkube.gitProviderConfigJson" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
{{- $result := list -}}
{{- $providers := include "greenkube.normalizedAutomationProviders" . | fromYamlArray -}}
{{- range $provider := $providers -}}
  {{- $secretName := get $provider "secretName" -}}
  {{- $secretKey := get $provider "secretKey" -}}
  {{- $secretNamespace := get $provider "secretNamespace" | default "" -}}
  {{- $apiBaseUrl := get $provider "apiBaseUrl" | default "" -}}
  {{- $tokenSecret := dict "name" $secretName "key" $secretKey -}}
  {{- if $secretNamespace -}}
    {{- $_ := set $tokenSecret "namespace" $secretNamespace -}}
  {{- end -}}
  {{- $result = append $result (dict
      "id" (get $provider "id")
      "type" (get $provider "type")
      "apiBaseUrl" $apiBaseUrl
      "tokenSecret" $tokenSecret
      "repositories" (get $provider "repositoryAllowlist")
    ) -}}
{{- end -}}
{{- toJson (dict "providers" $result) -}}
{{- end -}}

{{/*
Group Secret keys by Secret name so each referenced Secret is projected once.
*/}}
{{- define "greenkube.gitProviderSecretSources" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
{{- $secretKeys := dict -}}
{{- $providers := include "greenkube.normalizedAutomationProviders" . | fromYamlArray -}}
{{- range $provider := $providers -}}
  {{- $secretName := get $provider "secretName" -}}
  {{- $secretNamespace := get $provider "secretNamespace" | default "" -}}
  {{- $secretKey := get $provider "secretKey" -}}
  {{- $identity := printf "%s/%s" $secretNamespace $secretName -}}
  {{- $secret := get $secretKeys $identity | default (dict "name" $secretName "namespace" $secretNamespace "keys" (list)) -}}
  {{- $keys := get $secret "keys" | default (list) -}}
  {{- if not (has $secretKey $keys) -}}
    {{- $keys = append $keys $secretKey -}}
  {{- end -}}
  {{- $_ := set $secretKeys $identity (dict "name" $secretName "namespace" $secretNamespace "keys" $keys) -}}
{{- end -}}
{{- $sources := list -}}
{{- range $identity := keys $secretKeys | sortAlpha -}}
  {{- $secret := get $secretKeys $identity -}}
  {{- $items := list -}}
  {{- range $secretKey := get $secret "keys" -}}
    {{- $path := printf "%s/%s" (get $secret "name") $secretKey -}}
    {{- if get $secret "namespace" -}}
      {{- $path = printf "%s/%s" (get $secret "namespace") $path -}}
    {{- end -}}
    {{- $items = append $items (dict
        "key" $secretKey
        "path" $path
      ) -}}
  {{- end -}}
  {{- $sources = append $sources (dict "secret" (dict "name" (get $secret "name") "items" $items)) -}}
{{- end -}}
{{- toYaml $sources -}}
{{- end -}}

{{/*
Read-only mount for the API and automation-capable containers.
*/}}
{{- define "greenkube.gitProviderVolumeMount" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
{{- if .Values.automation.providers }}
- name: git-provider-secrets
  mountPath: /etc/greenkube/git-providers
  readOnly: true
{{- end -}}
{{- end -}}

{{/*
Projected volume with only the configured Secret keys.
*/}}
{{- define "greenkube.gitProviderVolume" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
{{- if .Values.automation.providers }}
- name: git-provider-secrets
  projected:
    sources:
      {{- include "greenkube.gitProviderSecretSources" . | nindent 6 }}
{{- end -}}
{{- end -}}
