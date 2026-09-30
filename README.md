# gcp-capacity-exporter

Prometheus exporter for the GCE Capacity Advisor, the Compute Engine **beta** advice APIs. It
exports Spot obtainability, estimated uptime, preemption rate, and price for an explicit list of
regions, machine types, VM counts, and target distribution shapes.

`advice/capacity` only returns a point-in-time score, and Google keeps no obtainability history.
The only way to get daily or weekly averages (`avg_over_time(...[24h])`) is to collect the scores
yourself; this exporter does that. The series are meant to feed ccc-costopt's planned averaged
capacity judgment, and you can also chart them directly.

## Metrics

All names start with `metric_prefix` (default `gce_capacity`).

| Metric | Type | Labels |
| --- | --- | --- |
| `gce_capacity_spot_hourly_price` | gauge, USD/hour | `region`, `machine_type`, `purchase_option="spot"` |
| `gce_capacity_spot_monthly_price` | gauge, USD/month | `region`, `machine_type`, `purchase_option="spot"` |
| `gce_capacity_obtainability_score` | gauge, 0-1 | `region`, `zone`, `machine_type`, `size`, `target_distribution_shape` |
| `gce_capacity_spot_preemption_rate` | gauge, 0-1 | `region`, `zone`, `machine_type` |
| `gce_capacity_estimated_uptime_seconds` | gauge, seconds | `region`, `zone`, `machine_type`, `size`, `target_distribution_shape` |
| `gce_capacity_requests_total` | counter | `api` |
| `gce_capacity_errors_total` | counter | `api`, `region`, `zone`, `machine_type`, `size`, `target_distribution_shape`, `reason` |
| `gce_capacity_scrape_duration_seconds` | gauge | none |

- **Price** is the `advice/capacityHistory` PRICE interval active now. If no interval covers
  now (the history has gaps when data is unavailable), the most recently started one is used.
  The monthly series is `hourly x hours_per_month` (default 730, the same default as ccc-costopt).
  Both come from one API call.
- **Obtainability** is the likelihood of getting `size` Spot VMs of the machine type. Google
  documents the bands as high >= 0.7 and medium >= 0.4. **Estimated uptime** is how long most
  of those VMs are expected to run before preemption; documented values are 60, 600, and 3600
  seconds.
- **Preemption rate** is the latest daily rate. Days start at midnight Pacific time, and the
  current day's rate is provisional (it changes during the day).
- **Region-level series** have an empty `zone` label, which Prometheus drops. Query them with
  `zone=""`. Per-zone series exist only for targets that list `zones`.
- Gauges are published once per cycle as one snapshot. `/metrics` never shows a half-finished
  cycle, and a series whose call failed disappears until a later cycle refreshes it, rather
  than repeating a stale value.
- `requests_total` counts every HTTP attempt, retries included, so it tracks quota use.
- `errors_total` counts calls that failed after retries, and calls that succeeded but returned
  no usable value for an enabled signal (`reason="no_data"`). Other reasons: `http_<status>`,
  `timeout`, `connection`, `auth`, `invalid_response`, `other`. History calls have an empty
  `size` and `target_distribution_shape`.

## What one cycle calls

Each target expands into these calls:

- **Capacity (region-level):** `machine_types x sizes x target_distribution_shapes` calls to
  `advice/capacity`, one machine type per call. A multi-type request returns a single blended
  score, which says nothing about the individual types.
- **Capacity (zonal):** with `zones`, one extra `ANY_SINGLE_ZONE` call per `zone x size x
  machine type`. A zone pin only makes sense with that shape, so `target_distribution_shapes`
  does not apply to zonal calls.
- **History (region-level):** one `advice/capacityHistory` call per `(region, machine type)`,
  fetching PRICE and PREEMPTION together. History does not depend on shape or size, so it
  costs the same however many of those you configure.
- **History (zonal):** with `zones`, one PREEMPTION-only call per `zone x machine type`. Prices
  are regional.

API volume therefore grows linearly with shapes and sizes. Identical calls across targets are
deduplicated. To see the per-cycle call count without calling any API, run:

```console
$ gcp-capacity-exporter --config config.example.yaml --check-config
config OK: project my-project, 1 target(s), signals: price, obtainability, preemption_rate, estimated_uptime
per cycle: 8 advice/capacity + 2 advice/capacityHistory calls (before retries), every 5m = ~120 calls/hour
  us-east4: 8 regional capacity (shapes: ANY, BALANCED), 0 zonal capacity, 2 history
```

The first cycle runs at startup. After that, cycles start on a fixed interval grid. A cycle
that overruns the interval skips the missed ticks instead of starting back-to-back.

## Configuration

[`config.example.yaml`](config.example.yaml) is the annotated reference. The whole file is
validated at startup, and unknown keys are errors: a leftover `target_shapes` fails with
"did you mean 'target_distribution_shapes'?".

| Key | Default | Notes |
| --- | --- | --- |
| `project_id` | required | project billed for the advice API calls |
| `listen` | `0.0.0.0:9469` | `host:port`; `--listen` overrides it |
| `metric_prefix` | `gce_capacity` | a trailing `_` is ignored |
| `scrape.interval` | `5m` | duration string (`30s`, `5m`, `1h30m`) |
| `scrape.timeout` | `30s` | per HTTP request |
| `scrape.workers` | `4` | concurrent API calls |
| `scrape.max_attempts` | `4` | per call, first try included |
| `hours_per_month` | `730` | integer >= 1 |
| `signals` | all four | subset of `price`, `obtainability`, `preemption_rate`, `estimated_uptime` |
| `targets[].region` | required | |
| `targets[].machine_types` | required | predefined types; the advice APIs reject custom ones |
| `targets[].sizes` | required with obtainability or estimated_uptime | VM counts to score |
| `targets[].target_distribution_shapes` | `[ANY]` | `ANY`, `ANY_SINGLE_ZONE`, `BALANCED`; one series per shape |
| `targets[].zones` | `[]` | zones of that region, for per-zone series |

Which API a signal needs decides which calls run. With `signals: [price]`, the exporter never
calls `advice/capacity`, and `sizes` becomes optional.

Retries cover HTTP 429, 500, 502, 503, and 504, rate-limit 403s, timeouts, and connection
errors, using exponential backoff with jitter and honouring `Retry-After`. A 401 refreshes the
token and retries once. Other errors fail the call on the first attempt.

## Authentication and IAM

The exporter uses [Application Default Credentials][adc] and never sends unauthenticated
requests. If no credentials are found at startup, it exits with status 1.

The identity needs `compute.advice.capacity` and `compute.advice.capacityHistory` on
`project_id`. Both are included in `roles/compute.viewer`.

On GKE, use Workload Identity. Either set `serviceAccount.gcpServiceAccount` in the chart to a
Google service account that the Kubernetes service account may impersonate, or grant the role
directly to the Kubernetes service account's principal.

[adc]: https://cloud.google.com/docs/authentication/application-default-credentials

## Running

Locally:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
gcloud auth application-default login
.venv/bin/gcp-capacity-exporter --config config.example.yaml --once   # one cycle, prints metrics
.venv/bin/gcp-capacity-exporter --config config.example.yaml          # serves :9469
```

`--once` exits non-zero if any call failed, which makes it a quick permissions and config
smoke test. The config path can also come from `$GCP_CAPACITY_EXPORTER_CONFIG`, and the log
level from `--log-level` or `$LOG_LEVEL`.

Endpoints:

- `/metrics` serves the Prometheus exposition (gzip and OpenMetrics negotiated). It serves the
  last published snapshot and never calls the API.
- `/healthz` returns 200 with JSON details of the last cycle. It returns 503 only when the
  scrape loop makes no progress for `2 x interval + max_attempts x timeout + 60s`. API errors
  never fail it, because a restart cannot fix them; alert on `errors_total` instead.

## Container image

```sh
docker build -t gcp-capacity-exporter:0.1.0 .
docker run --rm -p 9469:9469 --user "$(id -u)" \
  -v "$PWD/config.example.yaml:/etc/gcp-capacity-exporter/config.yaml:ro" \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
  gcp-capacity-exporter:0.1.0
```

The image is based on `python:3.14-slim`; pass `--build-arg PYTHON_IMAGE=<mirror>/python:3.14-slim`
to build from a registry mirror. It runs as UID 10001 by default (the `--user` above only lets
the container read your credentials file), and its default command reads
`/etc/gcp-capacity-exporter/config.yaml`.

## Helm chart

[`chart/`](chart) contains a Deployment (one replica), a ConfigMap rendered from `.Values.config`
(the pod restarts when it changes), a Service, a ServiceAccount with an optional Workload
Identity annotation, and an optional ServiceMonitor.

```sh
helm upgrade --install capacity-exporter ./chart -n monitoring \
  --set image.repository=us-east4-docker.pkg.dev/<project>/img/gcp-capacity-exporter \
  -f my-values.yaml
```

[`chart/ci/example-values.yaml`](chart/ci/example-values.yaml) shows a minimal `my-values.yaml`.
`config.project_id` and `config.targets` are required, and rendering fails without them. To
have Prometheus Operator scrape the exporter, set `serviceMonitor.enabled: true` and give
`serviceMonitor.labels` labels matching your Prometheus `serviceMonitorSelector` (for example
`prometheus: my-prometheus`).

## Using the series

Smoothed obtainability for a fleet of 50 VMs under shape ANY:

```promql
avg by (machine_type) (
  avg_over_time(gce_capacity_obtainability_score{region="us-east4", zone="", size="50", target_distribution_shape="ANY"}[24h])
)
```

A pod restart starts new series, because the `pod` and `instance` target labels change. Aggregate
with `avg by (...)`, as above, rather than relying on a single series.

ccc-costopt's existing `prometheus` price provider can read the monthly price with no code
change:

```yaml
pricing:
  provider: prometheus
  prometheus:
    url: ${PROM_URL}
    metric: gce_capacity_spot_monthly_price   # labels region, machine_type, purchase_option="spot"
    window: 24h
```

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
helm lint chart -f chart/ci/example-values.yaml
```

