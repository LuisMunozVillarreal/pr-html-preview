apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "pr-html-preview.fullname" . }}
  labels:
    {{- include "pr-html-preview.labels" . | nindent 4 }}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ include "pr-html-preview.name" . }}
      app.kubernetes.io/instance: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {{ include "pr-html-preview.name" . }}
        app.kubernetes.io/instance: {{ .Release.Name }}
        {{- with .Values.podLabels }}{{ toYaml . | nindent 8 }}{{- end }}
      {{- with .Values.podAnnotations }}
      annotations: {{ toYaml . | nindent 8 }}
      {{- end }}
    spec:
      serviceAccountName: {{ include "pr-html-preview.fullname" . }}
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{ toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: server
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: GITHUB_APP_ID
              value: {{ required "github.appId is required" .Values.github.appId | quote }}
            - name: GITHUB_PRIVATE_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ required "github.existingSecret is required" .Values.github.existingSecret }}
                  key: {{ .Values.github.privateKeyKey }}
            - name: GITHUB_WEBHOOK_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.github.existingSecret }}
                  key: {{ .Values.github.webhookSecretKey }}
            - name: PREVIEW_USERNAME_FILE
              value: /var/run/secrets/pr-html-preview-auth/username
            - name: PREVIEW_PASSWORD_FILE
              value: /var/run/secrets/pr-html-preview-auth/password
            - name: PREVIEW_BASE_URL
              value: {{ .Values.preview.baseUrl | quote }}
            - name: STORAGE_ROOT
              value: /data/previews

            - name: PREVIEW_DEFAULT_SOURCE_DIR
              value: {{ .Values.preview.defaultSourceDir | quote }}
            - name: PREVIEW_SOURCE_DIR_OVERRIDES
              value: {{ .Values.preview.sourceDirOverrides | toJson | quote }}
            - name: GITHUB_WEBHOOK_BODY_MAX_BYTES
              value: {{ printf "%.0f" .Values.preview.webhookBodyMaxBytes | quote }}
            - name: PREVIEW_MAX_FILE_COUNT
              value: {{ printf "%.0f" .Values.preview.maxFileCount | quote }}
            - name: PREVIEW_MAX_FILE_SIZE_BYTES
              value: {{ printf "%.0f" .Values.preview.maxFileSizeBytes | quote }}
            - name: PREVIEW_MAX_TOTAL_BYTES
              value: {{ printf "%.0f" .Values.preview.maxTotalBytes | quote }}
            - name: PREVIEW_MAX_STORAGE_BYTES
              value: {{ printf "%.0f" .Values.preview.maxStorageBytes | quote }}
            - name: PREVIEW_MAX_PREVIEW_COUNT
              value: {{ printf "%.0f" .Values.preview.maxPreviewCount | quote }}
            - name: PREVIEW_MIN_FREE_BYTES
              value: {{ printf "%.0f" .Values.preview.minFreeBytes | quote }}
          volumeMounts:
            - name: previews
              mountPath: /data/previews
            - name: tmp
              mountPath: /tmp
            - name: preview-auth
              mountPath: /var/run/secrets/pr-html-preview-auth
              readOnly: true
          livenessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 10
            periodSeconds: 20
          readinessProbe:
            httpGet: {path: /healthz, port: http}
            initialDelaySeconds: 2
            periodSeconds: 10
          resources: {{ toYaml .Values.resources | nindent 12 }}
      volumes:
        - name: previews
          persistentVolumeClaim:
            claimName: {{ include "pr-html-preview.fullname" . }}-data
        - name: tmp
          emptyDir: {}
        - name: preview-auth
          secret:
            secretName: {{ include "pr-html-preview.authSecretName" . }}
      {{- with .Values.nodeSelector }}
      nodeSelector: {{ toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.affinity }}
      affinity: {{ toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.tolerations }}
      tolerations: {{ toYaml . | nindent 8 }}
      {{- end }}
