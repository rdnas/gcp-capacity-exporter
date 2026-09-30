{{/*
Chart name, overridable with nameOverride.
*/}}
{{- define "gcp-capacity-exporter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name: the release name, suffixed with the chart name unless
it already contains it.
*/}}
{{- define "gcp-capacity-exporter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "gcp-capacity-exporter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "gcp-capacity-exporter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gcp-capacity-exporter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "gcp-capacity-exporter.labels" -}}
helm.sh/chart: {{ include "gcp-capacity-exporter.chart" . }}
{{ include "gcp-capacity-exporter.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "gcp-capacity-exporter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "gcp-capacity-exporter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
