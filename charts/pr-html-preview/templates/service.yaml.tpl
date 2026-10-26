apiVersion: v1
kind: Service
metadata:
  name: {{ include "pr-html-preview.fullname" . }}
  labels:
    {{- include "pr-html-preview.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {{ include "pr-html-preview.name" . }}
    app.kubernetes.io/instance: {{ .Release.Name }}
  ports:
    - name: http
      port: 80
      targetPort: http
