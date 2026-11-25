"""Serve the playground on loopback with the signed-in Azure CLI account."""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from azure.identity import AzureCliCredential

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from api.trace_api import MAX_BODY_BYTES, TraceTarget, handle_trace
from services import Http, uuid_setting


ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE.parent / "config.foundry.local.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = json.loads(args.config.read_bytes())
    tenant = uuid_setting(settings, "tenantId")
    target = TraceTarget.from_prefix(settings["searchEndpoint"], settings["indexPrefix"])
    origin = f"http://127.0.0.1:{args.port}"
    allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, status: int, body: dict):
            self.send(status, json.dumps(body).encode(), "application/json")

        def check_loopback(self) -> bool:
            host = self.headers.get("Host")
            if host not in allowed_hosts or self.headers.get("Origin") not in (None, "http://" + host):
                self.json_response(403, {"error": "The local server only accepts same-origin loopback requests."})
                return False
            return True

        def do_GET(self):
            if not self.check_loopback():
                return
            asset = ASSETS.get(self.path)
            if asset:
                self.send(200, (HERE / "web" / asset[0]).read_bytes(), asset[1])
            else:
                self.json_response(404, {"error": "Not found."})

        def do_POST(self):
            if not self.check_loopback():
                return
            if self.path != "/api/trace":
                self.json_response(404, {"error": "Not found."})
                return
            if self.headers.get_content_type() != "application/json":
                self.json_response(415, {"error": "Content-Type must be application/json."})
                return
            length = self.headers.get("Content-Length", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 9:
                self.json_response(400, {"error": "A valid Content-Length is required."})
                return
            if not 0 < int(length) <= MAX_BODY_BYTES:
                self.json_response(413, {"error": "The request is empty or too large."})
                return
            self.connection.settimeout(5)
            try:
                body = self.rfile.read(int(length))
            except TimeoutError:
                self.json_response(408, {"error": "Request body timed out."})
                return
            with AzureCliCredential(tenant_id=tenant, process_timeout=30) as credential:
                self.json_response(*handle_trace(body, target, Http(credential)))

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Manufacturing graph playground: {origin}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
