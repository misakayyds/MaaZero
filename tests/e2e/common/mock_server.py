"""Lightweight loopback HTTP test server for E2E testing."""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Optional, Tuple, Callable, Any
from urllib.parse import urlparse


class MockHTTPHandler(BaseHTTPRequestHandler):
    """Dynamic request handler for the mock loopback server."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP stderr logging."""
        pass

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_HEAD(self) -> None:
        self._handle_request("HEAD")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def _handle_request(self, method: str) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        server: MockServer = self.server  # type: ignore

        req_record = {
            "method": method,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        server.record_request(req_record)

        parsed = urlparse(self.path)
        route_handler = server.routes.get((method, parsed.path)) or server.routes.get(("*", parsed.path))

        if route_handler:
            status_code, headers, resp_body = route_handler(req_record)
        else:
            status_code, headers, resp_body = server.default_handler(req_record)

        self.send_response(status_code)
        for k, v in headers.items():
            self.send_header(k, v)
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()

        if method != "HEAD":
            self.wfile.write(resp_body)


class MockServer(HTTPServer):
    """Loopback HTTP server with route registration and request logging."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        super().__init__((host, port), MockHTTPHandler)
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.routes: Dict[Tuple[str, str], Callable[[Dict[str, Any]], Tuple[int, Dict[str, str], bytes]]] = {}
        self._thread: Optional[threading.Thread] = None
        self.default_handler = self._default_route

    def _default_route(self, req: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        return 200, {"Content-Type": "text/html; charset=utf-8"}, b"<!doctype html><html><body><h1>Mock Server</h1></body></html>"

    def record_request(self, req: Dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(req)

    def get_requests(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.requests)

    def reset_requests(self) -> None:
        with self._lock:
            self.requests.clear()

    def add_route(
        self,
        method: str,
        path: str,
        handler: Callable[[Dict[str, Any]], Tuple[int, Dict[str, str], bytes]],
    ) -> None:
        self.routes[(method, path)] = handler

    def add_json_route(self, method: str, path: str, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self.add_route(method, path, lambda r: (status, headers, body))

    def add_static_route(self, method: str, path: str, status: int, content_type: str, body: bytes) -> None:
        headers = {"Content-Type": content_type}
        self.add_route(method, path, lambda r: (status, headers, body))

    def start(self) -> "MockServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    @property
    def port(self) -> int:
        return self.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"
