apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ include "pr-html-preview.fullname" . }}-data
  labels:
    {{- include "pr-html-preview.labels" . | nindent 4 }}
  {{- if .Values.persistence.retain }}
  annotations:
    helm.sh/resource-policy: keep
  {{- end }}
spec:
  accessModes:
    - {{ .Values.persistence.accessMode }}
  {{- if .Values.persistence.storageClass }}
  storageClassName: {{ .Values.persistence.storageClass | quote }}
  {{- end }}
  resources:
    requests:
      storage: {{ .Values.persistence.size }}
