import unittest
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from multipart_fixture import MultipartFixture
from test_s3_fixture import request


def stage(server, key):
    started = request(server, "POST", key, query={"uploads": ""})
    assert started[0] == 200, started
    upload_id = ET.fromstring(started[1]).findtext("UploadId")
    part = request(server, "PUT", key, b"\x00\xff\x80", query={"partNumber": "1", "uploadId": upload_id})
    assert part[0] == 200, part
    complete = ("<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>" +
                escape(part[2]["ETag"]) + "</ETag></Part></CompleteMultipartUpload>").encode()
    return upload_id, complete


class MultipartFixtureTests(unittest.TestCase):
    def test_signed_queries_completion_cas_and_abort(self):
        with MultipartFixture() as server:
            key = "multi/space +%/café"
            upload_id, body = stage(server, key)
            first = request(server, "POST", key, body, {"if-none-match": "*"}, query={"uploadId": upload_id})
            self.assertEqual(first[0], 200)
            self.assertEqual(ET.fromstring(first[1]).tag, "CompleteMultipartUploadResult")
            self.assertEqual(request(server, "GET", key)[:2], (200, b"\x00\xff\x80"))
            upload_id, body = stage(server, key)
            conflict = request(server, "POST", key, body, {"if-none-match": "*"}, query={"uploadId": upload_id})
            self.assertEqual(conflict[0], 412)
            self.assertEqual(request(server, "DELETE", key, query={"uploadId": upload_id})[0], 204)
            self.assertEqual(server.state.uploads, {})
            self.assertEqual(server.state.snapshot()["rejected_signatures"], 0)

    def test_http_200_error_does_not_publish(self):
        with MultipartFixture() as server:
            key = "embedded-error/pack"
            upload_id, body = stage(server, key)
            result = request(server, "POST", key, body, {"if-none-match": "*"}, query={"uploadId": upload_id})
            self.assertEqual(result[0], 200)
            self.assertEqual(ET.fromstring(result[1]).tag, "Error")
            self.assertEqual(request(server, "GET", key)[0], 404)
            self.assertEqual(request(server, "DELETE", key, query={"uploadId": upload_id})[0], 204)


if __name__ == "__main__":
    unittest.main()