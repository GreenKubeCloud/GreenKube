{{/*
Effective component settings (roadmap F11, N3) with the 0.3.x values mapped
onto them for one minor release (F17). Every template reads the topology from
here, never from the deprecated keys:

  {{- $v := include "greenkube.values" . | fromYaml }}
  {{ $v.api.replicas }}

A deprecated key wins over the new default it maps to: an upgrade that reuses
a 0.3.x values file keeps its sizes. "deprecations" lists what was mapped (the
install notes print it). The values schema (N2) rejects these keys in the next
minor release.
*/}}
{{- define "greenkube.values" -}}
{{- $values := .Values -}}
{{- $api := deepCopy ($values.api | default dict) -}}
{{- $controller := deepCopy ($values.controller | default dict) -}}
{{- $renderer := deepCopy ($values.renderer | default dict) -}}
{{- $worker := deepCopy ($values.worker | default dict) -}}
{{- $automation := $values.automation | default dict -}}
{{- $policy := $automation.policy | default dict -}}
{{- $config := $values.config | default dict -}}
{{- $recommendations := $config.recommendations | default dict -}}
{{- $deprecations := list -}}
{{- $pauseDelivery := (get $policy "pauseDelivery") | default false -}}

{{- /* profile: one topology now. */ -}}
{{- if hasKey $values "profile" -}}
  {{- $deprecations = append $deprecations (printf "profile=%v is ignored: one topology (api, controller, renderer, PostgreSQL) replaces both profiles." $values.profile) -}}
{{- end -}}

{{- /* replicaCount: replicas of the standalone pod, which served the API. */ -}}
{{- if hasKey $values "replicaCount" -}}
  {{- $_ := set $api "replicas" (int $values.replicaCount) -}}
  {{- $deprecations = append $deprecations "replicaCount is mapped to api.replicas." -}}
{{- end -}}

{{- /* Standalone resources and probes. */ -}}
{{- if hasKey $values "resources" -}}
  {{- $_ := set $controller "resources" $values.resources -}}
  {{- $deprecations = append $deprecations "resources is mapped to controller.resources." -}}
{{- end -}}
{{- if hasKey $values "apiResources" -}}
  {{- $_ := set $api "resources" $values.apiResources -}}
  {{- $deprecations = append $deprecations "apiResources is mapped to api.resources." -}}
{{- end -}}
{{- with $values.collector -}}
  {{- if hasKey . "metricsPort" -}}
    {{- $_ := set $controller "metricsPort" .metricsPort -}}
  {{- end -}}
  {{- range $probe := list "livenessProbe" "readinessProbe" -}}
    {{- if hasKey $.Values.collector $probe -}}
      {{- $_ := set $controller $probe (mergeOverwrite (deepCopy (get $controller $probe | default dict)) (get $.Values.collector $probe)) -}}
    {{- end -}}
  {{- end -}}
  {{- $deprecations = append $deprecations "collector.* is mapped to controller.* (metricsPort and probes)." -}}
{{- end -}}
{{- $apiConfig := $config.api | default dict -}}
{{- range $probe := list "livenessProbe" "readinessProbe" -}}
  {{- if hasKey $apiConfig $probe -}}
    {{- $_ := set $api $probe (mergeOverwrite (deepCopy (get $api $probe | default dict)) (get $apiConfig $probe)) -}}
    {{- $deprecations = append $deprecations (printf "config.api.%s is mapped to api.%s." $probe $probe) -}}
  {{- end -}}
{{- end -}}
{{- if hasKey $apiConfig "enabled" -}}
  {{- $deprecations = append $deprecations "config.api.enabled is ignored: the API is always deployed (it serves the UI)." -}}
{{- end -}}

{{- /* Production profile block: it only shaped the 0.3.x production profile, so a
standalone release reused with --reuse-values (which carries the old chart defaults,
production block included) keeps the single-replica sizes. */ -}}
{{- $production := dict -}}
{{- if eq (toString ($values.profile | default "")) "production" -}}
  {{- $production = $values.production | default dict -}}
{{- else if hasKey $values "production" -}}
  {{- $deprecations = append $deprecations "production.* is ignored: it only applied to profile=production." -}}
{{- end -}}
{{- with $production -}}
  {{- $components := .components | default dict -}}
  {{- range $pair := list (list "api" $api) (list "controller" $controller) -}}
    {{- $legacy := get $components (index $pair 0) | default dict -}}
    {{- $target := index $pair 1 -}}
    {{- range $key := list "replicas" "resources" "livenessProbe" "readinessProbe" -}}
      {{- if hasKey $legacy $key -}}
        {{- $value := get $legacy $key -}}
        {{- if kindIs "map" $value -}}
          {{- $value = omit $value "path" "port" -}}
          {{- $_ := set $target $key (mergeOverwrite (deepCopy (get $target $key | default dict)) $value) -}}
        {{- else -}}
          {{- $_ := set $target $key $value -}}
        {{- end -}}
      {{- end -}}
    {{- end -}}
  {{- end -}}
  {{- with $components.automation -}}
    {{- if hasKey . "resources" -}}
      {{- $_ := set $worker "resources" .resources -}}
    {{- end -}}
  {{- end -}}
  {{- with .leaderElection -}}
    {{- $election := deepCopy ($controller.leaderElection | default dict) -}}
    {{- range $key := list "leaseName" "leaseDurationSeconds" -}}
      {{- if hasKey $production.leaderElection $key -}}
        {{- $_ := set $election $key (get $production.leaderElection $key) -}}
      {{- end -}}
    {{- end -}}
    {{- $_ := set $controller "leaderElection" $election -}}
  {{- end -}}
  {{- $deprecations = append $deprecations "production.* is mapped to api.*, controller.*, worker.* and controller.leaderElection.*; production.migration is ignored (the controller migrates the database at startup)." -}}
{{- end -}}

{{- /* automation.enabled / automation.mode (F12): delivery is always on. */ -}}
{{- $legacyMode := "" -}}
{{- if hasKey $automation "mode" -}}
  {{- $legacyMode = $automation.mode | default "" | toString -}}
  {{- $deprecations = append $deprecations (printf "automation.mode=%s is removed: the controller always executes plans (worker.replicas adds dedicated workers)." ($legacyMode | default "''")) -}}
{{- end -}}
{{- if hasKey $automation "enabled" -}}
  {{- if not $automation.enabled -}}
    {{- $pauseDelivery = true -}}
    {{- $deprecations = append $deprecations "automation.enabled=false is mapped to automation.policy.pauseDelivery=true: discovery, previews and measurement run, no pull request is opened." -}}
  {{- else -}}
    {{- $deprecations = append $deprecations "automation.enabled is removed: automation is always on and activates itself." -}}
  {{- end -}}
{{- end -}}
{{- if eq $legacyMode "disabled" -}}
  {{- $pauseDelivery = true -}}
{{- end -}}
{{- $legacyProfile := $values.profile | default "standalone" | toString -}}
{{- $legacyEnabled := and (hasKey $automation "enabled") $automation.enabled -}}
{{- if and $legacyEnabled (or (eq $legacyMode "worker") (and (eq $legacyMode "") (eq $legacyProfile "production"))) -}}
  {{- $replicas := 1 -}}
  {{- with ($production.components | default dict).automation -}}
    {{- if hasKey . "replicas" -}}
      {{- $replicas = int .replicas -}}
    {{- end -}}
  {{- end -}}
  {{- $_ := set $worker "replicas" $replicas -}}
{{- end -}}
{{- if hasKey $automation "workerMetricsPort" -}}
  {{- $_ := set $worker "metricsPort" $automation.workerMetricsPort -}}
  {{- $deprecations = append $deprecations "automation.workerMetricsPort is mapped to worker.metricsPort." -}}
{{- end -}}

{{- /* rendererSandbox: the renderer is always deployed. */ -}}
{{- with $values.rendererSandbox -}}
  {{- range $key := list "replicas" "servicePort" "tmpSizeLimit" "resources" "egressFirewall" -}}
    {{- if hasKey $.Values.rendererSandbox $key -}}
      {{- $_ := set $renderer $key (get $.Values.rendererSandbox $key) -}}
    {{- end -}}
  {{- end -}}
  {{- $deprecations = append $deprecations "rendererSandbox.* is mapped to renderer.*; the renderer is always deployed (rendererSandbox.enabled is ignored)." -}}
{{- end -}}

{{- /* SQLite values (F11, F17): PostgreSQL only. */ -}}
{{- $db := $config.db | default dict -}}
{{- if hasKey $db "type" -}}
  {{- if eq (toString $db.type) "sqlite" -}}
    {{- fail (include "greenkube.sqliteUpgradeMessage" .) -}}
  {{- end -}}
  {{- $deprecations = append $deprecations "config.db.type is removed: the chart always uses PostgreSQL." -}}
{{- end -}}
{{- if hasKey $db "path" -}}
  {{- $deprecations = append $deprecations "config.db.path is ignored: SQLite is for `greenkube demo` and local development only." -}}
{{- end -}}
{{- if hasKey $config "persistence" -}}
  {{- $deprecations = append $deprecations "config.persistence is deprecated: the former SQLite PersistentVolumeClaim is kept (helm.sh/resource-policy: keep) until you delete it after `greenkube db copy`." -}}
{{- end -}}

{{- /* One minSamples knob (N3). */ -}}
{{- $minSamples := int ($recommendations.minSamples | default 36) -}}
{{- $policyMinSamples := $minSamples -}}
{{- $verificationMinSamples := $minSamples -}}
{{- if hasKey $policy "minSamples" -}}
  {{- $policyMinSamples = int $policy.minSamples -}}
  {{- $deprecations = append $deprecations "automation.policy.minSamples is deprecated: config.recommendations.minSamples sets every sample gate." -}}
{{- end -}}
{{- $verification := $recommendations.verification | default dict -}}
{{- if hasKey $verification "minSamples" -}}
  {{- $verificationMinSamples = int $verification.minSamples -}}
  {{- $deprecations = append $deprecations "config.recommendations.verification.minSamples is deprecated: config.recommendations.minSamples sets every sample gate." -}}
{{- end -}}

{{- /* Sources grouped under sources.* (N3, E5). */ -}}
{{- $sources := $values.sources | default dict -}}
{{- $sourcePriority := $sources.priority | default "vpa,karpenter,greenkube" -}}
{{- if hasKey $recommendations "sourcePriority" -}}
  {{- $sourcePriority = $recommendations.sourcePriority -}}
  {{- $deprecations = append $deprecations "config.recommendations.sourcePriority is mapped to sources.priority." -}}
{{- end -}}
{{- $vpaEnabled := "true" -}}
{{- $karpenterEnabled := "true" -}}
{{- if hasKey $recommendations "vpaEnabled" -}}
  {{- $vpaEnabled = $recommendations.vpaEnabled | toString -}}
  {{- $deprecations = append $deprecations "config.recommendations.vpaEnabled is deprecated: use sources.settings.vpa.enabled." -}}
{{- end -}}
{{- if hasKey $recommendations "karpenterEnabled" -}}
  {{- $karpenterEnabled = $recommendations.karpenterEnabled | toString -}}
  {{- $deprecations = append $deprecations "config.recommendations.karpenterEnabled is deprecated: use sources.settings.karpenter.enabled." -}}
{{- end -}}

{{- $_ := set $api "port" (int ($apiConfig.port | default 8000)) -}}
{{- if not (hasKey $controller "replicas") -}}
  {{- $_ := set $controller "replicas" 1 -}}
{{- end -}}
{{- $_ := set $controller "replicas" (int $controller.replicas) -}}
{{- dict
    "api" $api
    "controller" $controller
    "renderer" $renderer
    "worker" $worker
    "pauseDelivery" $pauseDelivery
    "minSamples" $minSamples
    "policyMinSamples" $policyMinSamples
    "verificationMinSamples" $verificationMinSamples
    "sourcePriority" $sourcePriority
    "vpaEnabled" $vpaEnabled
    "karpenterEnabled" $karpenterEnabled
    "deprecations" $deprecations
  | toYaml -}}
{{- end -}}

{{/*
The pre-upgrade validation of a 0.3.x SQLite install (F17): rendering fails
with the exact commands, before Helm touches anything.
*/}}
{{- define "greenkube.sqliteUpgradeMessage" -}}
{{- $fullname := include "greenkube.fullname" . -}}
{{- $path := ((.Values.config | default dict).db | default dict).path | default "/data/greenkube_data.db" -}}
config.db.type=sqlite is no longer supported: the chart runs on PostgreSQL only (GreenKube 0.4).
Your SQLite data stays on the PersistentVolumeClaim {{ $fullname }}. Migrate in three steps (docs/upgrading.md#from-a-sqlite-install):
  1. helm upgrade {{ .Release.Name }} <chart> -n {{ .Release.Namespace }} --reset-then-reuse-values --set config.db.type=postgres --set controller.replicas=0
     (keep config.persistence.enabled=true: the claim is then kept, never deleted)
  2. run the one-shot Job of docs/upgrading.md, which mounts the claim at /data and runs:
     greenkube db copy --from sqlite://{{ $path }} --to "$DB_CONNECTION_STRING"
  3. helm upgrade {{ .Release.Name }} <chart> -n {{ .Release.Namespace }} --reset-then-reuse-values --set controller.replicas=1
{{- end -}}
