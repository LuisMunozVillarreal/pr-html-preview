apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "pr-html-preview.fullname" . }}
  labels:
    {{- include "pr-html-preview.labels" . | nindent 4 }}
automountServiceAccountToken: false
