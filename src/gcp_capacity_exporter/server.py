"""HTTP endpoints: /metrics (Prometheus exposition) and /healthz (scrape loop liveness)."""

from __future__ import annotations

import json
import socket
import threading
from socketserver import ThreadingMixIn
from typing import Any, Callable, Iterable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import CollectorRegistry, make_wsgi_app

HealthCheck = Callable[[], tuple[bool, dict[str, Any]]]

_INDEX = b"""<html><head><title>gcp-capacity-exporter</title></head><body>
<h1>gcp-capacity-exporter</h1>
<p><a href="/metrics">/metrics</a> &middot; <a href="/healthz">/healthz</a></p>
</body></html>
"""


def make_app(registry: CollectorRegistry, health: HealthCheck) -> Callable:
    metrics_app = make_wsgi_app(registry)  # content negotiation, gzip, name[] filters

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        path = environ.get("PATH_INFO") or "/"
        if path == "/metrics":
            return metrics_app(environ, start_response)
        if path == "/healthz":
            ok, detail = health()
            body = json.dumps({"status": "ok" if ok else "unhealthy", **detail}).encode() + b"\n"
            status = "200 OK" if ok else "503 Service Unavailable"
            return _respond(start_response, status, "application/json", body)
        if path == "/":
            return _respond(start_response, "200 OK", "text/html; charset=utf-8", _INDEX)
        return _respond(
            start_response, "404 Not Found", "text/plain; charset=utf-8", b"not found\n"
        )

    return app


class MetricsServer:
    """Serves the app from a background thread."""

    def __init__(self, host: str, port: int, registry: CollectorRegistry, health: HealthCheck):
        server_class = type(
            "_Server", (_ThreadingWSGIServer,), {"address_family": _address_family(host, port)}
        )
        self._httpd = make_server(
            host, port, make_app(registry, health),
            server_class=server_class, handler_class=_QuietHandler,
        )
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="http", daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> MetricsServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Prometheus scrapes every few seconds; per-request access logs are noise."""


def _respond(start_response: Callable, status: str, content_type: str, body: bytes) -> list[bytes]:
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body)))])
    return [body]


def _address_family(host: str, port: int) -> socket.AddressFamily:
    infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    return infos[0][0]
