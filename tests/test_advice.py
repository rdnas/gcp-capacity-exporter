"""Advice client: request bodies, response parsing, retries, auth, HTTP transport."""

import json
import socket
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import google.auth
import pytest
from google.auth.exceptions import DefaultCredentialsError, RefreshError

from conftest import (
    NOW,
    FakeAPI,
    StaticTokens,
    capacity_doc,
    make_client,
    money,
    preemption_day,
    price_interval,
)
from gcp_capacity_exporter.advice import (
    MAX_RETRY_AFTER_S,
    AdviceClient,
    AdviceError,
    AuthError,
    Cancelled,
    GoogleTokenSource,
    RequestsTransport,
    active_hourly_price,
    latest_preemption_rate,
    parse_duration_s,
    parse_money,
    parse_timestamp,
)

BASE = "https://compute.googleapis.com/compute/beta/projects/my-project/regions/us-east4"


def http_error(status: int, **kwargs) -> AdviceError:
    retryable = status in (429, 500, 502, 503, 504)
    return AdviceError(
        f"HTTP {status}", reason=f"http_{status}", retryable=retryable, status=status, **kwargs
    )


def raising(error: Exception):
    def handler(call):
        raise error

    return handler


# -- request bodies -------------------------------------------------------------


def test_capacity_region_level_request_and_scores():
    api = FakeAPI(capacity=lambda call: capacity_doc(0.82, "3600s"))
    scores = make_client(api).capacity("us-east4", "t2d-standard-8", 50, "BALANCED")
    assert (scores.obtainability, scores.estimated_uptime_s) == (0.82, 3600.0)
    (call,) = api.calls
    assert call["url"] == f"{BASE}/advice/capacity"
    assert call["body"] == {
        "instanceProperties": {"scheduling": {"provisioningModel": "SPOT"}},
        "instanceFlexibilityPolicy": {
            "instanceSelections": {"selection-1": {"machineTypes": ["t2d-standard-8"]}}
        },
        "distributionPolicy": {"targetShape": "BALANCED"},
        "size": 50,
    }
    assert call["headers"]["authorization"] == "Bearer test-token"
    assert call["headers"]["Content-Type"] == "application/json"


def test_capacity_zonal_request_pins_the_zone():
    api = FakeAPI()
    make_client(api).capacity("us-east4", "t2d-standard-8", 10, "ANY_SINGLE_ZONE", zone="us-east4-a")
    assert api.calls[0]["body"]["distributionPolicy"] == {
        "targetShape": "ANY_SINGLE_ZONE",
        "zones": [{"zone": "zones/us-east4-a"}],
    }


@pytest.mark.parametrize(
    "doc", [{}, {"recommendations": []}, {"recommendations": "none"}]
)
def test_capacity_without_recommendation_is_no_data(doc):
    with pytest.raises(AdviceError) as info:
        make_client(FakeAPI(capacity=lambda call: doc)).capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "no_data"


def test_capacity_tolerates_missing_scores():
    doc = {"recommendations": [{"shards": []}]}
    scores = make_client(FakeAPI(capacity=lambda call: doc)).capacity("us-east4", "t2d-standard-8", 1)
    assert (scores.obtainability, scores.estimated_uptime_s) == (None, None)


def test_history_region_request_fetches_price_and_preemption_in_one_call():
    api = FakeAPI()
    signals = make_client(api).history("us-east4", "t2d-standard-8", price=True, preemption=True)
    assert signals.hourly_price_usd == Decimal("0.05")
    assert signals.preemption_rate == 0.03
    (call,) = api.calls
    assert call["url"] == f"{BASE}/advice/capacityHistory"
    assert call["body"] == {
        "types": ["PRICE", "PREEMPTION"],
        "instanceProperties": {
            "machineType": "t2d-standard-8",
            "scheduling": {"provisioningModel": "SPOT"},
        },
    }


def test_history_zonal_request_is_preemption_only():
    api = FakeAPI()
    signals = make_client(api).history(
        "us-east4", "t2d-standard-8", price=False, preemption=True, zone="us-east4-a"
    )
    assert signals.hourly_price_usd is None and signals.preemption_rate == 0.03
    body = api.calls[0]["body"]
    assert body["types"] == ["PREEMPTION"]
    assert body["locationPolicy"] == {"location": "zones/us-east4-a"}


def test_history_argument_validation():
    client = make_client(FakeAPI())
    with pytest.raises(ValueError, match="regional"):
        client.history("us-east4", "t2d-standard-8", price=True, preemption=False, zone="us-east4-a")
    with pytest.raises(ValueError, match="price, preemption, or both"):
        client.history("us-east4", "t2d-standard-8", price=False, preemption=False)


# -- parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"currencyCode": "USD", "units": "1", "nanos": 250000000}, Decimal("1.25")),
        ({"currencyCode": "USD", "nanos": "478720000"}, Decimal("0.47872")),  # docs example
        ({"units": "2"}, Decimal("2")),
        ({"units": "-1", "nanos": -500000000}, Decimal("-1.5")),
        ({"currencyCode": "USD"}, None),
        ({"units": "lots"}, None),
        ("0.05", None),
        (None, None),
    ],
)
def test_parse_money(value, expected):
    assert parse_money(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("600s", 600.0),
        ("3.5s", 3.5),
        ("3600", 3600.0),
        (60, 60.0),
        ({"seconds": "600"}, 600.0),
        ({"seconds": 1, "nanos": 500000000}, 1.5),
        ("soon", None),
        ("-5s", None),
        (None, None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_parse_duration(value, expected):
    assert parse_duration_s(value) == expected


def test_parse_timestamp():
    assert parse_timestamp("2026-04-20T07:00:00Z") == datetime(2026, 4, 20, 7, tzinfo=timezone.utc)
    nanos = parse_timestamp("2026-04-20T07:00:00.123456789Z")
    assert nanos == datetime(2026, 4, 20, 7, 0, 0, 123456, tzinfo=timezone.utc)
    assert parse_timestamp("2026-04-20T07:00:00").tzinfo == timezone.utc
    assert parse_timestamp("yesterday") is None
    assert parse_timestamp(None) is None


def test_active_price_prefers_the_interval_covering_now():
    history = [
        price_interval("2026-09-01T07:00:00Z", "2026-09-20T07:00:00Z", "0.03"),
        price_interval("2026-09-20T07:00:00Z", "2026-10-01T07:00:00Z", "0.05"),
    ]
    assert active_hourly_price(history, NOW) == Decimal("0.05")
    open_ended = [price_interval("2026-09-20T07:00:00Z", None, "0.04")]
    assert active_hourly_price(open_ended, NOW) == Decimal("0.04")


def test_active_price_falls_back_to_the_latest_started_interval_across_a_gap():
    history = [
        price_interval("2026-09-25T07:00:00Z", "2026-09-28T07:00:00Z", "0.06"),
        price_interval("2026-09-01T07:00:00Z", "2026-09-25T07:00:00Z", "0.03"),
    ]
    assert active_hourly_price(history, NOW) == Decimal("0.06")


def test_active_price_ignores_future_non_usd_and_non_positive_entries():
    history = [
        price_interval("2026-09-01T07:00:00Z", None, "0.05"),
        price_interval("2026-10-01T07:00:00Z", None, "0.01"),  # announced, not active yet
        price_interval("2026-09-29T07:00:00Z", None, "0.99", currency="EUR"),
        {"interval": {"startTime": "2026-09-29T07:00:00Z"}, "listPrice": {"currencyCode": "USD"}},
        price_interval("2026-09-30T07:00:00Z", None, "0.0"),
        {"listPrice": money("0.02")},  # no interval
        "garbage",
    ]
    assert active_hourly_price(history, NOW) == Decimal("0.05")
    assert active_hourly_price([], NOW) is None
    assert active_hourly_price(None, NOW) is None


def test_latest_preemption_rate_orders_by_interval_not_position():
    history = [
        preemption_day("2026-09-30", 0.07),
        preemption_day("2026-09-28", 0.50),
        preemption_day("2026-09-29", 0.20),
    ]
    assert latest_preemption_rate(history) == 0.07


def test_latest_preemption_rate_tolerates_schema_variants():
    assert latest_preemption_rate([{"rate": 0.02}, {"value": "0.04"}]) == 0.04  # last wins
    assert latest_preemption_rate([preemption_day("2026-09-30", 0.1), {"preemptionRate": "n/a"}]) == 0.1
    assert latest_preemption_rate([]) is None
    assert latest_preemption_rate(None) is None


# -- retries and auth -------------------------------------------------------------


def test_retryable_errors_back_off_and_every_attempt_is_counted():
    attempts = []

    def flaky(call):
        attempts.append(call)
        if len(attempts) < 3:
            raise http_error(503)
        return capacity_doc(0.5)

    slept, sent = [], []
    client = make_client(FakeAPI(capacity=flaky), sleep=slept.append, on_request=sent.append)
    assert client.capacity("us-east4", "t2d-standard-8", 10).obtainability == 0.5
    assert sent == ["capacity"] * 3
    assert len(slept) == 2 and slept[1] > slept[0]


def test_gives_up_after_max_attempts_and_keeps_the_reason():
    sent = []
    client = make_client(
        FakeAPI(capacity=raising(http_error(429))), max_attempts=3, on_request=sent.append
    )
    with pytest.raises(AdviceError, match="gave up after 3 attempts") as info:
        client.capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "http_429"
    assert len(sent) == 3


@pytest.mark.parametrize("retry_after, expected", [(7.0, 7.0), (1000.0, MAX_RETRY_AFTER_S)])
def test_retry_after_is_honoured_and_capped(retry_after, expected):
    slept = []
    client = make_client(
        FakeAPI(capacity=raising(http_error(429, retry_after_s=retry_after))),
        max_attempts=2,
        sleep=slept.append,
    )
    with pytest.raises(AdviceError):
        client.capacity("us-east4", "t2d-standard-8", 10)
    assert slept == [expected]


def test_non_retryable_errors_fail_fast():
    sent = []
    client = make_client(FakeAPI(capacity=raising(http_error(403))), on_request=sent.append)
    with pytest.raises(AdviceError) as info:
        client.capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "http_403"
    assert "gave up" not in str(info.value)
    assert len(sent) == 1


def test_401_refreshes_the_token_once_without_spending_an_attempt():
    responses = [http_error(401), capacity_doc(0.7)]

    def expired_then_ok(call):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    tokens, sent = StaticTokens(), []
    client = make_client(
        FakeAPI(capacity=expired_then_ok), tokens=tokens, max_attempts=1, on_request=sent.append
    )
    assert client.capacity("us-east4", "t2d-standard-8", 10).obtainability == 0.7
    assert tokens.invalidations == 1
    assert len(sent) == 2

    always_401 = make_client(FakeAPI(capacity=raising(http_error(401))), max_attempts=1)
    with pytest.raises(AdviceError) as info:
        always_401.capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "http_401"


def test_token_failures_are_retried_and_do_not_count_as_requests():
    class FlakyTokens(StaticTokens):
        calls = 0

        def headers(self):
            self.calls += 1
            if self.calls == 1:
                raise AuthError("metadata server unavailable")
            return super().headers()

    sent = []
    client = make_client(FakeAPI(), tokens=FlakyTokens(), on_request=sent.append)
    assert client.capacity("us-east4", "t2d-standard-8", 10).obtainability == 0.8
    assert sent == ["capacity"]


def test_shutdown_cancels_before_anything_is_sent():
    api, sent = FakeAPI(), []
    client = make_client(api, should_stop=lambda: True, on_request=sent.append)
    with pytest.raises(Cancelled):
        client.capacity("us-east4", "t2d-standard-8", 10)
    assert api.calls == [] and sent == []


class FakeCredentials:
    def __init__(self, fail: bool = False):
        self.valid = False
        self.token = None
        self.refreshes = 0
        self.fail = fail

    def refresh(self, request):
        if self.fail:
            raise RefreshError("metadata server unavailable")
        self.refreshes += 1
        self.token = f"token-{self.refreshes}"
        self.valid = True

    def apply(self, headers, token=None):
        headers["authorization"] = f"Bearer {token or self.token}"
        headers["x-goog-user-project"] = "quota-project"


def test_google_token_source_refreshes_lazily_and_on_invalidate():
    credentials = FakeCredentials()
    tokens = GoogleTokenSource(credentials)
    assert tokens.headers() == {
        "authorization": "Bearer token-1",
        "x-goog-user-project": "quota-project",
    }
    tokens.headers()
    assert credentials.refreshes == 1  # still valid: no refresh
    tokens.invalidate()
    assert tokens.headers()["authorization"] == "Bearer token-2"


def test_google_token_source_refresh_failure_is_an_auth_error():
    with pytest.raises(AuthError, match="could not refresh") as info:
        GoogleTokenSource(FakeCredentials(fail=True)).headers()
    assert info.value.reason == "auth"


def test_missing_adc_is_a_hard_auth_error(monkeypatch):
    def no_credentials(**kwargs):
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(google.auth, "default", no_credentials)
    with pytest.raises(AuthError, match="no usable Application Default Credentials"):
        GoogleTokenSource.from_adc()


# -- HTTP transport ------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.requests.append(json.loads(self.rfile.read(length) or b"null"))
        script = self.server.script
        status, body, headers, delay = script.pop(0) if len(script) > 1 else script[0]
        if delay:
            time.sleep(delay)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that timed out leaves a broken pipe behind


@pytest.fixture
def server():
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.script, httpd.requests = [], []
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def http_client(server, **kwargs) -> AdviceClient:
    kwargs.setdefault("sleep", lambda seconds: None)
    return AdviceClient(
        "my-project",
        StaticTokens(),
        transport=RequestsTransport(),
        base_url=f"http://127.0.0.1:{server.server_address[1]}/compute/beta",
        **kwargs,
    )


def test_http_transport_retries_503_then_parses_the_response(server):
    server.script = [(503, b"unavailable", {}, 0), (200, capacity_doc(0.82), {}, 0)]
    scores = http_client(server).capacity("us-east4", "t2d-standard-8", 50)
    assert scores.obtainability == 0.82
    assert len(server.requests) == 2
    assert server.requests[0]["size"] == 50


def test_http_transport_reports_google_error_details(server):
    error = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "Required 'compute.advice.capacity' permission for 'projects/my-project'",
        }
    }
    server.script = [(403, error, {}, 0)]
    with pytest.raises(AdviceError) as info:
        http_client(server).capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "http_403" and not info.value.retryable
    assert "PERMISSION_DENIED: Required 'compute.advice.capacity' permission" in str(info.value)
    assert len(server.requests) == 1


def test_http_transport_retries_rate_limit_403s_and_reads_retry_after(server):
    error = {"error": {"code": 403, "message": "Rate Limit Exceeded",
                       "errors": [{"reason": "rateLimitExceeded"}]}}
    server.script = [(403, error, {"Retry-After": "3"}, 0), (200, capacity_doc(), {}, 0)]
    slept = []
    http_client(server, sleep=slept.append).capacity("us-east4", "t2d-standard-8", 10)
    assert slept and slept[0] >= 3
    assert len(server.requests) == 2


def test_http_transport_invalid_json_timeout_and_connection_errors(server):
    server.script = [(200, b"<html>proxy error</html>", {}, 0)]
    with pytest.raises(AdviceError) as info:
        http_client(server, max_attempts=1).capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "invalid_response"

    server.script = [(200, capacity_doc(), {}, 0.5)]
    with pytest.raises(AdviceError) as info:
        http_client(server, max_attempts=1, timeout_s=0.1).capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "timeout"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    client = AdviceClient(
        "my-project",
        StaticTokens(),
        transport=RequestsTransport(),
        base_url=f"http://127.0.0.1:{closed_port}/compute/beta",
        max_attempts=1,
    )
    with pytest.raises(AdviceError) as info:
        client.capacity("us-east4", "t2d-standard-8", 10)
    assert info.value.reason == "connection"
