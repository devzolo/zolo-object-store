"""Multipart fault fixture; authenticated requests, parts and final CAS are checked."""
import hashlib
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from s3_fixture import Handler, Object, S3Fixture


class MultipartHandler(Handler):
    def query(self):
        return dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))

    def do_GET(self):
        key = urlsplit(self.path).path
        if "/truncated/" not in key:
            return super().do_GET()
        key = self.object_request(b"")
        if key is None:
            return
        with self.state.lock:
            obj = self.state.objects[key]
            self.send_response(200)
            self.send_header("ETag", obj.etag)
            self.send_header("Content-Length", str(len(obj.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(obj.body[:len(obj.body) // 2])
            self.close_connection = True

    def do_PUT(self):
        query = self.query()
        if not query:
            return super().do_PUT()
        body = self.read_body()
        key = self.object_request(body)
        if key is None:
            return
        with self.state.lock:
            self.state.counters["multipart_parts"] += 1
            failure = self.state.next_failure(key)
            if failure:
                self.injected_reply(failure)
                return
            upload = self.state.uploads.get(query.get("uploadId"))
            if upload is None or upload["key"] != key:
                self.reply(404)
                return
            if self.headers.get("If-Match") or self.headers.get("If-None-Match"):
                self.reply(400, b"part must not carry CAS")
                return
            number = int(query["partNumber"])
            if not 1 <= number <= 10000:
                self.reply(400)
                return
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            upload["parts"][number] = Object(body, etag)
            self.reply(200, etag=etag)

    def do_POST(self):
        if self.path == "/__fixture/control":
            return super().do_POST()
        body = self.read_body()
        key = self.object_request(body)
        if key is None:
            return
        query = self.query()
        with self.state.lock:
            if query == {"uploads": ""}:
                self.state.counters["multipart_started"] += 1
                upload_id = "upload /+=" + str(self.state.counters["multipart_started"])
                self.state.uploads[upload_id] = {"key": key, "parts": {}}
                self.reply(200, ("<InitiateMultipartUploadResult><UploadId>" +
                           escape(upload_id) + "</UploadId></InitiateMultipartUploadResult>").encode())
                return
        upload_id = query.get("uploadId")
        if self.state.before_complete:
            self.state.before_complete(key, body, self.headers)
        with self.state.lock:
            self.state.counters["multipart_complete_calls"] += 1
            upload = self.state.uploads.get(upload_id)
            if upload is None or upload["key"] != key:
                self.reply(404)
                return
            if key.startswith("embedded-error/"):
                self.reply(200, b"<Error><Code>InternalError</Code><ETag>&quot;fake&quot;</ETag></Error>")
                return
            try:
                root = ET.fromstring(body)
                assert root.tag == "CompleteMultipartUpload"
                parts = []
                for expected, node in enumerate(root.findall("Part"), 1):
                    assert int(node.findtext("PartNumber")) == expected
                    part = upload["parts"][expected]
                    assert node.findtext("ETag") == part.etag
                    parts.append(part.body)
                assert parts and all(len(part) >= 5 * 1024 * 1024 for part in parts[:-1])
            except (AssertionError, KeyError, TypeError, ET.ParseError):
                self.reply(400)
                return
            create = self.headers.get("If-None-Match")
            expected = self.headers.get("If-Match")
            if (create == "*") == (expected is not None):
                self.reply(400, b"completion requires exactly one condition")
                return
            current = self.state.objects.get(key)
            if (create == "*" and current is not None) or (
                    expected is not None and (current is None or current.etag != expected)):
                self.state.counters["conflicts"] += 1
                self.reply(412)
                return
            obj = self.state.write(key, b"".join(parts))
            del self.state.uploads[upload_id]
            self.state.counters["multipart_completed"] += 1
            if key.startswith("ambiguous-complete/"):
                self.close_connection = True
                self.connection.close()
                return
            self.reply(200, ("<CompleteMultipartUploadResult><ETag>" +
                       escape(obj.etag) + "</ETag></CompleteMultipartUploadResult>").encode())

    def do_DELETE(self):
        body = self.read_body()
        key = self.object_request(body)
        if key is None:
            return
        with self.state.lock:
            self.state.counters["multipart_abort_calls"] += 1
            upload_id = self.query().get("uploadId")
            upload = self.state.uploads.get(upload_id)
            if upload is None:
                self.reply(404)
                return
            if upload["key"] != key:
                self.reply(404)
                return
            del self.state.uploads[upload_id]
            self.reply(204)


class MultipartFixture(S3Fixture):
    def __init__(self, **kwargs):
        super().__init__(handler_class=MultipartHandler, **kwargs)
        self.state.uploads = {}
        self.state.before_complete = None
        self.state.counters.update(multipart_started=0, multipart_parts=0,
                                   multipart_completed=0, multipart_complete_calls=0,
                                   multipart_abort_calls=0)