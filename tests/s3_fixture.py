"""Local S3 protocol fixture for object_store/gitwall integration tests.

This is a test fixture, not an S3 emulator or production storage server.
It verifies SigV4 and serializes conditional GET/PUT operations under one lock.
It binds only to loopback. Defaults match README test configuration.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlsplit

ACCESS_KEY = "gitwall-test"
SECRET_KEY = "gitwall-test-secret"
BUCKET = "gitwall"


@dataclass
class Object:
    body: bytes
    etag: str


@dataclass
class Failure:
    prefix: str
    status: int
    remaining: int = 1
    after_commit: bool = False
    disconnect: bool = False


class State:
    def __init__(self, data_dir=None, access_key=ACCESS_KEY, secret_key=SECRET_KEY,
                 region="us-east-1", session_token=None):
        self.lock = threading.RLock()
        self.objects: dict[str, Object] = {}
        self.failures: list[Failure] = []
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.session_token = session_token
        self.data_dir = Path(data_dir) if data_dir else None
        self.counters = {"get": 0, "put": 0, "writes": 0, "conflicts": 0,
                         "not_modified": 0, "missing": 0, "rejected_signatures": 0,
                         "injected_failures": 0}
        self.put_keys: list[str] = []
        self.signature_errors: list[str] = []
        self.before_put = None
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for path in self.data_dir.glob("*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                self.objects[record["key"]] = Object(
                    base64.b64decode(record["body"], validate=True), record["etag"])

    def write(self, key, body):
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        obj = Object(body, etag)
        if self.data_dir:
            name = hashlib.sha256(key.encode()).hexdigest()
            destination = self.data_dir / (name + ".json")
            temporary = self.data_dir / (name + ".tmp")
            temporary.write_text(json.dumps({
                "key": key, "body": base64.b64encode(body).decode(), "etag": etag,
            }), encoding="utf-8")
            temporary.replace(destination)
        self.objects[key] = obj
        self.counters["writes"] += 1
        return obj

    def fail_next_put(self, prefix, status=503, count=1, after_commit=False, disconnect=False):
        if not isinstance(prefix, str) or not 400 <= status <= 599 or not 1 <= count <= 100:
            raise ValueError("failure requires string prefix, HTTP 4xx/5xx and count 1..100")
        with self.lock:
            self.failures.append(Failure(prefix, status, count, after_commit, disconnect))

    def next_failure(self, key):
        for failure in self.failures:
            if failure.remaining and key.startswith(failure.prefix):
                failure.remaining -= 1
                self.counters["injected_failures"] += 1
                return failure
        return None

    def snapshot(self):
        with self.lock:
            return {**self.counters, "put_keys": list(self.put_keys),
                    "objects": sorted(self.objects), "signature_errors": list(self.signature_errors)}


def _signature(secret_key, day, region, to_sign):
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def verify_signature(handler, body, state):
    authorization = handler.headers.get("Authorization", "")
    match = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=([^/]+)/([0-9]{8})/([^/]+)/s3/aws4_request,"
        r"\s*SignedHeaders=([a-z0-9;-]+),\s*Signature=([0-9a-f]{64})", authorization)
    if not match:
        raise ValueError("invalid SigV4 Authorization")
    access_key, day, region, signed, signature = match.groups()
    if access_key != state.access_key or region != state.region:
        raise ValueError("wrong access key or signing region")
    names = signed.split(";")
    if names != sorted(set(names)):
        raise ValueError("SignedHeaders must be unique and sorted")
    required = {"host", "x-amz-content-sha256", "x-amz-date"}
    required.update(name.lower() for name in handler.headers
                    if name.lower().startswith("x-amz-") or
                    name.lower() in {"if-match", "if-none-match"})
    if not required.issubset(names):
        raise ValueError("required authentication/conditional header is unsigned")
    timestamp = handler.headers.get("x-amz-date", "")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", timestamp) or not timestamp.startswith(day):
        raise ValueError("invalid signing timestamp")
    when = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if abs((datetime.now(timezone.utc) - when).total_seconds()) > 900:
        raise ValueError("signing timestamp is outside the 15-minute test window")
    token = handler.headers.get("x-amz-security-token")
    if token != state.session_token:
        raise ValueError("wrong session token")
    digest = hashlib.sha256(body).hexdigest()
    if handler.headers.get("x-amz-content-sha256") != digest:
        raise ValueError("payload SHA-256 mismatch")
    lines = []
    for name in names:
        values = handler.headers.get_all(name)
        if values is None or len(values) != 1:
            raise ValueError("missing or duplicate signed header")
        lines.append(name + ":" + " ".join(values[0].split()) + "\n")
    uri = urlsplit(handler.path)
    if uri.fragment:
        raise ValueError("request fragments are not supported")
    pairs = [(quote(k, safe="-_.~"), quote(v, safe="-_.~"))
             for k, v in parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)]
    canonical_query = "&".join(k + "=" + v for k, v in sorted(pairs))
    canonical = (handler.command + "\n" + uri.path + "\n" + canonical_query + "\n" +
                 "".join(lines) + "\n" + signed + "\n" + digest)
    scope = day + "/" + region + "/s3/aws4_request"
    to_sign = ("AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" +
               hashlib.sha256(canonical.encode()).hexdigest())
    expected = _signature(state.secret_key, day, region, to_sign)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("signature mismatch")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def state(self):
        return self.server.state

    def log_message(self, fmt, *args):
        # Requests may contain credentials in headers; never log them.
        return

    def reply(self, status, body=b"", etag=None, content_type="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.close_connection = True
        if body:
            self.wfile.write(body)

    def json_reply(self, status, value):
        self.reply(status, json.dumps(value).encode(), content_type="application/json")

    def read_body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("fixture requires Content-Length, not chunked transfer")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= length <= 128 * 1024 * 1024:
            raise ValueError("invalid or excessive request length")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("truncated request body")
        return body

    def object_request(self, body):
        try:
            verify_signature(self, body, self.state)
        except (ValueError, OverflowError) as error:
            with self.state.lock:
                self.state.counters["rejected_signatures"] += 1
                self.state.signature_errors.append(str(error))
            self.json_reply(403, {"error": str(error)})
            return None
        path = urlsplit(self.path).path
        prefix = "/" + BUCKET + "/"
        if not path.startswith(prefix) or len(path) == len(prefix):
            self.json_reply(404, {"error": "fixture bucket/key not found"})
            return None
        try:
            return unquote_to_bytes(path[len(prefix):]).decode("utf-8", errors="strict")
        except UnicodeError:
            self.json_reply(400, {"error": "object keys must be UTF-8"})
            return None

    def do_GET(self):
        if self.path == "/__fixture/stats":
            self.json_reply(200, self.state.snapshot())
            return
        key = self.object_request(b"")
        if key is None:
            return
        with self.state.lock:
            self.state.counters["get"] += 1
            obj = self.state.objects.get(key)
            if obj is None:
                self.state.counters["missing"] += 1
                self.reply(404)
            elif self.headers.get("If-None-Match") == obj.etag:
                self.state.counters["not_modified"] += 1
                self.reply(304, etag=obj.etag)
            else:
                self.reply(200, obj.body, obj.etag)

    def injected_reply(self, failure):
        if failure.disconnect:
            self.close_connection = True
            self.connection.close()
        else:
            self.json_reply(failure.status, {"error": "injected test failure"})

    def do_PUT(self):
        try:
            body = self.read_body()
        except (ValueError, OverflowError) as error:
            self.json_reply(400, {"error": str(error)})
            return
        key = self.object_request(body)
        if key is None:
            return
        if self.state.before_put is not None:
            self.state.before_put(key, body, self.headers)
        with self.state.lock:
            self.state.counters["put"] += 1
            self.state.put_keys.append(key)
            failure = self.state.next_failure(key)
            if failure and not failure.after_commit:
                self.injected_reply(failure)
                return
            create = self.headers.get("If-None-Match")
            etag = self.headers.get("If-Match")
            if (create != "*" or etag is not None) and (create is not None or etag is None):
                self.json_reply(400, {"error": "PUT requires exactly one supported condition"})
                return
            current = self.state.objects.get(key)
            if (create == "*" and current is not None) or (
                    etag is not None and (current is None or current.etag != etag)):
                self.state.counters["conflicts"] += 1
                self.reply(412)
                return
            obj = self.state.write(key, body)
            if failure:
                self.injected_reply(failure)
                return
            self.reply(200, etag=obj.etag)

    def do_POST(self):
        # Local-only fixture control endpoint, deliberately not an S3 operation.
        if self.path != "/__fixture/control":
            self.reply(404)
            return
        try:
            command = json.loads(self.read_body())
            if command.get("operation") != "fail_next_put":
                raise ValueError("unknown fixture operation")
            self.state.fail_next_put(
                command["prefix"], int(command.get("status", 503)),
                int(command.get("count", 1)), bool(command.get("after_commit", False)),
                bool(command.get("disconnect", False)))
            self.json_reply(200, {"ok": True})
        except (ValueError, KeyError, TypeError) as error:
            self.json_reply(400, {"error": str(error)})


def create_server(port=0, data_dir=None, access_key=ACCESS_KEY, secret_key=SECRET_KEY,
                  region="us-east-1", session_token=None, handler_class=Handler):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
    server.daemon_threads = True
    server.state = State(data_dir, access_key, secret_key, region, session_token)
    return server


class S3Fixture:
    """Importable context manager; use .url, .state and .state.fail_next_put(...)."""
    def __init__(self, port=0, data_dir=None, **credentials):
        self.server = create_server(port, data_dir, **credentials)
        self.state = self.server.state
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--access-key", default=ACCESS_KEY)
    parser.add_argument("--secret-key", default=SECRET_KEY)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--session-token")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zolo-s3-fixture-") as temporary:
        server = create_server(args.port, args.data or temporary, args.access_key,
                               args.secret_key, args.region, args.session_token)
        ready = {"port": server.server_port,
                 "endpoint": "http://127.0.0.1:" + str(server.server_port), "bucket": BUCKET}
        if args.ready_file:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text(json.dumps(ready), encoding="utf-8")
        print(json.dumps(ready), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
