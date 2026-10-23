{{/*
Validate provider-specific webhook Secret references.
*/}}
{{- define "greenkube.validateAutomationWebhooks" -}}
{{- $webhooks := .Values.automation.webhooks -}}
{{- if not (kindIs "map" $webhooks) -}}
  {{- fail "automation.webhooks must be a map" -}}
{{- end -}}
{{- include "greenkube.validateAutomationProviders" . -}}
{{- $enabled := get $webhooks "enabled" -}}
{{- if not (kindIs "bool" $enabled) -}}
  {{- fail "automation.webhooks.enabled must be a boolean" -}}
{{- end -}}
{{- if $enabled -}}
  {{- $providers := .Values.automation.providers | default (list) -}}
  {{- $providerSecretCount := 0 -}}
  {{- $seenSecretRefs := dict -}}
  {{- $providerIDs := list -}}
  {{- range $provider := $providers -}}
    {{- $providerIDs = append $providerIDs (get $provider "id") -}}
    {{- $secretName := get $provider "webhookSecretName" -}}
    {{- $secretKey := get $provider "webhookSecretKey" -}}
    {{- if or $secretName $secretKey -}}
      {{- if or (not $secretName) (not $secretKey) -}}
        {{- fail "Each provider webhook Secret requires both webhookSecretName and webhookSecretKey" -}}
      {{- end -}}
      {{- $secretNamespace := get $provider "webhookSecretNamespace" | default $.Release.Namespace -}}
      {{- $identity := printf "%s/%s:%s" $secretNamespace $secretName $secretKey -}}
      {{- if hasKey $seenSecretRefs $identity -}}
        {{- fail "Each provider must use a distinct webhook Secret key" -}}
      {{- end -}}
      {{- $_ := set $seenSecretRefs $identity true -}}
      {{- $providerSecretCount = add1 $providerSecretCount -}}
    {{- end -}}
  {{- end -}}

  {{- $secretName := get $webhooks "secretName" | default "" -}}
  {{- $secretKey := get $webhooks "secretKey" | default "" -}}
  {{- $providerId := get $webhooks "providerId" | default "" -}}
  {{- $hasLegacySecret := or (ne $secretName "") (ne $secretKey "") -}}
  {{- $namespace := get $webhooks "secretNamespace" | default "" -}}
  {{- if $hasLegacySecret -}}
    {{- if gt $providerSecretCount 0 -}}
      {{- fail "Use either automation.webhooks.secretName/secretKey for one provider or per-provider webhook Secret references" -}}
    {{- end -}}
    {{- if or (not (kindIs "string" $secretName)) (gt (len $secretName) 253) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$" $secretName)) -}}
      {{- fail "automation.webhooks.secretName must be a valid Kubernetes Secret name" -}}
    {{- end -}}
    {{- if or (not (kindIs "string" $secretKey)) (gt (len $secretKey) 253) (not (regexMatch "^[A-Za-z0-9._-]+$" $secretKey)) (has $secretKey (list "." "..")) -}}
      {{- fail "automation.webhooks.secretKey must be a valid Kubernetes Secret key" -}}
    {{- end -}}
    {{- if $namespace -}}
      {{- if or (not (kindIs "string" $namespace)) (gt (len $namespace) 63) (not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $namespace)) -}}
        {{- fail "automation.webhooks.secretNamespace must be a valid Kubernetes namespace" -}}
      {{- end -}}
      {{- if ne $namespace $.Release.Namespace -}}
        {{- fail "automation.webhooks.secretNamespace must match the Helm release namespace" -}}
      {{- end -}}
    {{- end -}}
    {{- if not $providerId -}}
      {{- if eq (len $providers) 1 -}}
        {{- $providerId = get (first $providers) "id" -}}
      {{- else -}}
        {{- fail "automation.webhooks.providerId is required when using a shared webhook Secret without exactly one provider" -}}
      {{- end -}}
    {{- end -}}
    {{- if or (not (kindIs "string" $providerId)) (gt (len $providerId) 128) (not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" $providerId)) -}}
      {{- fail "automation.webhooks.providerId must be a valid provider ID" -}}
    {{- end -}}
    {{- if and (gt (len $providers) 0) (not (has $providerId $providerIDs)) -}}
      {{- fail "automation.webhooks.providerId must match a configured automation provider" -}}
    {{- end -}}
  {{- else -}}
    {{- if $providerId -}}
      {{- fail "automation.webhooks.providerId requires automation.webhooks.secretName and secretKey" -}}
    {{- end -}}
    {{- if $namespace -}}
      {{- fail "automation.webhooks.secretNamespace requires a shared webhook Secret reference" -}}
    {{- end -}}
    {{- if eq $providerSecretCount 0 -}}
      {{- fail "automation.webhooks.enabled requires at least one provider webhook Secret reference" -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- define "greenkube.webhookSecretMountPath" -}}/etc/greenkube/webhooks{{- end -}}
{{- define "greenkube.webhookSecretFilePath" -}}
{{- include "greenkube.webhookSecretMountPath" . -}}
{{- end -}}

{{/*
Read-only mount for provider-specific webhook keys; only API containers use it.
*/}}
{{- define "greenkube.webhookSecretVolumeMount" -}}
{{- include "greenkube.validateAutomationWebhooks" . -}}
{{- if .Values.automation.webhooks.enabled }}
- name: webhook-secret
  mountPath: {{ include "greenkube.webhookSecretMountPath" . }}
  readOnly: true
{{- end -}}
{{- end -}}

{{/*
Project each configured provider's webhook key to a provider-ID filename.
*/}}
{{- define "greenkube.webhookSecretSources" -}}
{{- $sources := list -}}
{{- $webhooks := .Values.automation.webhooks -}}
{{- if $webhooks.enabled -}}
  {{- $providers := .Values.automation.providers | default (list) -}}
  {{- $secretName := get $webhooks "secretName" | default "" -}}
  {{- $secretKey := get $webhooks "secretKey" | default "" -}}
  {{- if or $secretName $secretKey -}}
    {{- $providerId := get $webhooks "providerId" | default "" -}}
    {{- if not $providerId -}}
      {{- $providerId = get (first $providers) "id" -}}
    {{- end -}}
    {{- $sources = append $sources (dict
        "secret" (dict
          "name" $secretName
          "items" (list (dict "key" $secretKey "path" $providerId))
        )
      ) -}}
  {{- else -}}
    {{- range $provider := $providers -}}
      {{- $providerSecretName := get $provider "webhookSecretName" -}}
      {{- if $providerSecretName -}}
        {{- $sources = append $sources (dict
            "secret" (dict
              "name" $providerSecretName
              "items" (list (dict
                "key" (get $provider "webhookSecretKey")
                "path" (get $provider "id")
              ))
            )
          ) -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
{{- end -}}
{{- toYaml $sources -}}
{{- end -}}

{{/*
Projected volume containing only the selected key for each provider.
*/}}
{{- define "greenkube.webhookSecretVolume" -}}
{{- include "greenkube.validateAutomationWebhooks" . -}}
{{- if .Values.automation.webhooks.enabled }}
- name: webhook-secret
  projected:
    sources:
      {{- include "greenkube.webhookSecretSources" . | nindent 6 }}
{{- end -}}
{{- end -}}
