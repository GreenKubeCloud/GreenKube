{{/*
Return the configured providers, rejecting the removed alias keys with a migration hint.
*/}}
{{- define "greenkube.normalizedAutomationProviders" -}}
{{- $providers := .Values.automation.providers | default (list) -}}
{{- if not (kindIs "slice" $providers) -}}
{{- fail "automation.providers must be a list" -}}
{{- end -}}
{{- range $index, $provider := $providers -}}
  {{- if not (kindIs "map" $provider) -}}
    {{- fail (printf "automation.providers[%d] must be a map" $index) -}}
  {{- end -}}
  {{- if hasKey $provider "tokenSecret" -}}
    {{- fail (printf "automation.providers[%d].tokenSecret was removed; use secretName, secretKey and secretNamespace" $index) -}}
  {{- end -}}
  {{- if hasKey $provider "repositories" -}}
    {{- fail (printf "automation.providers[%d].repositories was removed; use repositoryAllowlist" $index) -}}
  {{- end -}}
{{- end -}}
{{- toYaml $providers -}}
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
  {{- if and (hasKey $provider "webBaseUrl") (not (kindIs "string" (get $provider "webBaseUrl"))) -}}
    {{- fail (printf "automation.providers[%d].webBaseUrl must be a string" $index) -}}
  {{- end -}}
  {{- $webBaseUrl := get $provider "webBaseUrl" | default "" -}}
  {{- if and $webBaseUrl (not (regexMatch "^https?://[^/?#@]+/?$" $webBaseUrl)) -}}
    {{- fail (printf "automation.providers[%d].webBaseUrl must be an HTTP(S) origin URL without a path" $index) -}}
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

  {{- $allowlist := get $provider "repositoryAllowlist" | default $.Values.automation.repositoryAllowlist -}}
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
  {{- $webBaseUrl := get $provider "webBaseUrl" | default "" -}}
  {{- $tokenSecret := dict "name" $secretName "key" $secretKey -}}
  {{- if $secretNamespace -}}
    {{- $_ := set $tokenSecret "namespace" $secretNamespace -}}
  {{- end -}}
  {{- $config := dict
      "id" (get $provider "id")
      "type" (get $provider "type")
      "apiBaseUrl" $apiBaseUrl
      "tokenSecret" $tokenSecret
      "repositories" (get $provider "repositoryAllowlist" | default $.Values.automation.repositoryAllowlist)
  -}}
  {{- if $webBaseUrl -}}
    {{- $_ := set $config "webBaseUrl" $webBaseUrl -}}
  {{- end -}}
  {{- $result = append $result $config -}}
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
{{- /* The default credentials Secret (F12) is optional: creating it later
activates delivery without a restart, because the kubelet refreshes projected
Secret volumes. */ -}}
{{- $defaultSecret := include "greenkube.gitCredentialsSecretName" . -}}
{{- $defaultIdentity := printf "/%s" $defaultSecret -}}
{{- $defaultKeys := get $secretKeys $defaultIdentity | default (dict "keys" (list)) -}}
{{- $keys := get $defaultKeys "keys" | default (list) -}}
{{- range $key := list "token" "appId" "privateKey" -}}
  {{- if not (has $key $keys) -}}
    {{- $keys = append $keys $key -}}
  {{- end -}}
{{- end -}}
{{- $_ := set $secretKeys $defaultIdentity (dict "name" $defaultSecret "namespace" "" "keys" $keys "optional" true) -}}
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
  {{- $source := dict "name" (get $secret "name") "items" $items -}}
  {{- if get $secret "optional" -}}
    {{- $_ := set $source "optional" true -}}
  {{- end -}}
  {{- $sources = append $sources (dict "secret" $source) -}}
{{- end -}}
{{- toYaml $sources -}}
{{- end -}}

{{/*
Name of the Secret read for inferred providers (F12).
*/}}
{{- define "greenkube.gitCredentialsSecretName" -}}
{{- .Values.automation.credentialsSecret | default "greenkube-git-credentials" -}}
{{- end -}}

{{/*
Read-only mount for the API, controller and worker containers. Always present:
the default credentials Secret may appear at any time (F12).
*/}}
{{- define "greenkube.gitProviderVolumeMount" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
- name: git-provider-secrets
  mountPath: /etc/greenkube/git-providers
  readOnly: true
{{- end -}}

{{/*
Projected volume with only the configured Secret keys.
*/}}
{{- define "greenkube.gitProviderVolume" -}}
{{- include "greenkube.validateAutomationProviders" . -}}
- name: git-provider-secrets
  projected:
    sources:
      {{- include "greenkube.gitProviderSecretSources" . | nindent 6 }}
{{- end -}}
