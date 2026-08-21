{{- define "pr-html-preview.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pr-html-preview.fullname" -}}
{{- if .Values.fullnameOverride }}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}{{ else }}{{ printf "%s-%s" .Release.Name (include "pr-html-preview.name" .) | trunc 63 | trimSuffix "-" }}{{ end }}
{{- end }}

{{- define "pr-html-preview.labels" -}}
app.kubernetes.io/name: {{ include "pr-html-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "pr-html-preview.authSecretName" -}}
{{- default (printf "%s-preview-auth" (include "pr-html-preview.fullname" .)) .Values.preview.existingAuthSecret -}}
{{- end }}
