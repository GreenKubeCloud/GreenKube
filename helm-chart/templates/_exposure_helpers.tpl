{{/*
Optional exposure of the UI and API (roadmap N6): an Ingress (ingress.*) or a
Gateway API HTTPRoute (httpRoute.*), both routing "/" of their hosts to the api Service (the UI and the API share
it and are served from the root path).

"greenkube.externalUrl" is GREENKUBE_EXTERNAL_URL, the base of the deep links
in pull requests, commit statuses and status comments (J1):
  1. config.externalUrl when set (always wins);
  2. otherwise the first ingress host when the Ingress is enabled, https when a
     tls entry lists that host (or lists no hosts), http otherwise;
  3. otherwise the first HTTPRoute hostname, with httpRoute.scheme (https);
  4. otherwise empty: no links. A wildcard first host ("*.example.com")
     names no single origin and builds no URL.
*/}}
{{- define "greenkube.externalUrl" -}}
{{- $explicit := (.Values.config | default dict).externalUrl | default "" -}}
{{- $ingress := .Values.ingress | default dict -}}
{{- $route := .Values.httpRoute | default dict -}}
{{- $url := "" -}}
{{- if $explicit -}}
  {{- $url = $explicit -}}
{{- else if and $ingress.enabled $ingress.hosts (not (hasPrefix "*" (first $ingress.hosts))) -}}
  {{- $host := first $ingress.hosts -}}
  {{- $scheme := "http" -}}
  {{- range $tls := ($ingress.tls | default list) -}}
    {{- if or (not $tls.hosts) (has $host $tls.hosts) -}}
      {{- $scheme = "https" -}}
    {{- end -}}
  {{- end -}}
  {{- $url = printf "%s://%s" $scheme $host -}}
{{- else if and $route.enabled $route.hostnames (not (hasPrefix "*" (first $route.hostnames))) -}}
  {{- $url = printf "%s://%s" ($route.scheme | default "https") (first $route.hostnames) -}}
{{- end -}}
{{- $url | trimSuffix "/" -}}
{{- end -}}

