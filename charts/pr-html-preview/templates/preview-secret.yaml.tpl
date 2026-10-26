{{- if not .Values.preview.existingAuthSecret -}}
{{- $name := include "pr-html-preview.authSecretName" . -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $name -}}
{{- $password := randAlphaNum 40 -}}
{{- if and $existing (hasKey $existing.data "password") -}}
{{- $password = (index $existing.data "password" | b64dec) -}}
{{- end -}}
apiVersion: v1
kind: Secret
metadata:
  name: {{ $name }}
  labels:
    {{- include "pr-html-preview.labels" . | nindent 4 }}
type: Opaque
stringData:
  username: {{ .Values.preview.username | quote }}
  password: {{ $password | quote }}
{{- end }}
