{{/*
Scheduling, availability and helper-image helpers (production readiness).

Every workload reads its own block (api, controller, worker, renderer, postgres,
postgres.backup) for nodeSelector, tolerations, affinity,
topologySpreadConstraints, priorityClassName, podAnnotations and podLabels;
image pull secrets are global (global.imagePullSecrets).
*/}}

{{/*
A helper image (busybox, postgres): "<repository>:<tag>". The default tags
carry their digest ("18-alpine@sha256:..."), so overriding the tag replaces the
pin with it; an explicit "digest" key pins any tag ("<repository>:<tag>@<digest>").
  {{ include "greenkube.helperImage" .Values.postgres.image }}
*/}}
{{- define "greenkube.helperImage" -}}
{{- $repository := required "a helper image repository is required" .repository -}}
{{- $tag := .tag | default "" | toString -}}
{{- $digest := .digest | default "" | toString -}}
{{- if and $digest (not (regexMatch "^sha256:[0-9a-fA-F]{64}$" $digest)) -}}
  {{- fail (printf "image digest %q for %s must use the form sha256:<64 hexadecimal characters>" $digest $repository) -}}
{{- end -}}
{{- $ref := $repository -}}
{{- if $tag -}}{{- $ref = printf "%s:%s" $ref $tag -}}{{- end -}}
{{- if and $digest (contains "@" $tag) -}}
  {{- fail (printf "image %s: set either a tag with @sha256 or a digest, not both" $repository) -}}
{{- end -}}
{{- if $digest -}}{{- $ref = printf "%s@%s" $ref $digest -}}{{- end -}}
{{- $ref -}}
{{- end -}}

{{/*
imagePullSecrets of every pod, from global.imagePullSecrets: a list of names
or of {name: ...} entries. Renders nothing when empty.
  {{- include "greenkube.imagePullSecrets" $root | nindent 6 }}
*/}}
{{- define "greenkube.imagePullSecrets" -}}
{{- include "greenkube.imagePullSecrets.raw" . | trim -}}
{{- end -}}

{{- define "greenkube.imagePullSecrets.raw" -}}
{{- $secrets := list -}}
{{- range ((.Values.global | default dict).imagePullSecrets | default list) -}}
  {{- if kindIs "string" . -}}
    {{- $secrets = append $secrets (dict "name" .) -}}
  {{- else -}}
    {{- $secrets = append $secrets (dict "name" (required "global.imagePullSecrets entries need a name" .name)) -}}
  {{- end -}}
{{- end -}}
{{- with $secrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end -}}
{{- end -}}

{{/*
Pod template labels: user podLabels first, then the selector labels, which
always win (a podLabels entry can never detach a pod from its Deployment).
  {{- include "greenkube.podLabels" (dict "context" $root "cfg" $cfg "component" "api") | nindent 8 }}
*/}}
{{- define "greenkube.podLabels" -}}
{{- $selector := include "greenkube.selectorLabels" .context | fromYaml -}}
{{- $_ := set $selector "app.kubernetes.io/component" .component -}}
{{- $labels := mergeOverwrite (deepCopy (.cfg.podLabels | default dict)) $selector -}}
{{- toYaml $labels -}}
{{- end -}}

{{/*
Pod scheduling fields: priorityClassName, nodeSelector, tolerations, affinity
and topologySpreadConstraints. With more than one replica and no explicit
affinity, replicas prefer distinct nodes (podAntiAffinity: soft, the default),
must use distinct nodes (hard) or are not spread (none).
  {{- include "greenkube.podScheduling" (dict "context" $root "cfg" $cfg "component" "api" "replicas" 2) | nindent 6 }}
*/}}
{{- define "greenkube.podScheduling" -}}
{{- include "greenkube.podScheduling.raw" . | trim -}}
{{- end -}}

{{- define "greenkube.podScheduling.raw" -}}
{{- $cfg := .cfg | default dict -}}
{{- $replicas := int (.replicas | default 1) -}}
{{- with $cfg.priorityClassName }}
priorityClassName: {{ . | quote }}
{{- end }}
{{- with $cfg.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $cfg.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if $cfg.affinity }}
affinity:
  {{- toYaml $cfg.affinity | nindent 2 }}
{{- else if gt $replicas 1 }}
{{- $mode := $cfg.podAntiAffinity | default "soft" | toString }}
{{- $term := dict "topologyKey" "kubernetes.io/hostname" "labelSelector" (dict "matchLabels" (include "greenkube.podLabels" (dict "context" .context "cfg" dict "component" .component) | fromYaml)) }}
{{- if eq $mode "soft" }}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          {{- toYaml $term | nindent 10 }}
{{- else if eq $mode "hard" }}
affinity:
  podAntiAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      - {{- toYaml $term | nindent 8 }}
{{- else if ne $mode "none" }}
{{- fail (printf "%s.podAntiAffinity must be soft, hard or none (got %q)" .component $mode) }}
{{- end }}
{{- end }}
{{- with $cfg.topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
