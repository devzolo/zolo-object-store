"""Contract tests for the local S3 fixture; no external service is contacted."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from s3_fixture import S3Fixture, ACCESS_KEY, SECRET_KEY, _signature


def request(fixture, method, key, body=b"", condition=None, tamper=False, unsigned_condition=False, query=None):
    path = "/gitwall/" + quote(key, safe="/-_.~")
    query_text = "&".join(quote(k, safe="-_.~") + "=" + quote(v, safe="-_.~") for k, v in sorted((query or {}).items()))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = timestamp[:8]
    scope = day + "/us-east-1/s3/aws4_request"
    digest = hashlib.sha256(body).hexdigest()
    headers = {"host": urlsplit(fixture.url).netloc, "x-amz-date": timestamp,
               "x-amz-content-sha256": digest}
    if condition and not unsigned_condition:
        headers.update(condition)
    names = sorted(headers)
    signed = ";".join(names)
    canonical = (method + "\n" + path + "\n" + query_text + "\n" +
                 "".join(name + ":" + headers[name] + "\n" for name in names) +
                 "\n" + signed + "\n" + digest)
    to_sign = "AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    signing_key = ("AWS4" + SECRET_KEY).encode()
    for value in (day, "us-east-1", "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    if tamper:
        signature = "0" * 64
    headers["authorization"] = ("AWS4-HMAC-SHA256 Credential=" + ACCESS_KEY + "/" + scope +
                                ",SignedHeaders=" + signed + ",Signature=" + signature)
    if condition and unsigned_condition:
        headers.update(condition)
    req = Request(fixture.url + path + ("?" + query_text if query_text else ""),
                  data=body if method in {"PUT", "POST", "DELETE"} else None,
                  headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, response.read(), response.headers
    except HTTPError as response:
        return response.code, response.read(), response.headers


class FixtureTests(unittest.TestCase):
    def test_published_aws_signature(self):
        signing = ("AWS4-HMAC-SHA256\n20130524T000000Z\n20130524/us-east-1/s3/aws4_request\n"
                   "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972")
        self.assertEqual(_signature("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                   "20130524", "us-east-1", signing),
                         "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_binary_cas_signature_and_restart(self):
        with tempfile.TemporaryDirectory() as data:
            with S3Fixture(data_dir=data) as server:
                key = "pack/space +%/caf" + chr(233)
                self.assertEqual(request(server, "GET", key)[0], 404)
                first = request(server, "PUT", key, bytes([0, 255, 128]), {"if-none-match": "*"})
                self.assertEqual(first[0], 200)
                etag = first[2]["ETag"]
                found = request(server, "GET", key)
                self.assertEqual(found[:2], (200, bytes([0, 255, 128])))
                self.assertEqual(request(server, "GET", key, condition={"if-none-match": etag})[0], 304)
                self.assertEqual(request(server, "PUT", key, b"x", {"if-none-match": "*"})[0], 412)
                self.assertEqual(request(server, "PUT", key, b"x", {"if-match": '"old"'})[0], 412)
                self.assertEqual(request(server, "PUT", key, b"x", {"if-match": etag}, tamper=True)[0], 403)
                self.assertEqual(request(server, "PUT", key, b"x", {"if-match": etag}, unsigned_condition=True)[0], 403)
                self.assertEqual(request(server, "PUT", key, b"new", {"if-match": etag})[0], 200)
                self.assertEqual(server.state.snapshot()["rejected_signatures"], 2)
            with S3Fixture(data_dir=data) as restarted:
                self.assertEqual(request(restarted, "GET", key)[:2], (200, b"new"))

    def test_write_failure_before_and_after_commit(self):
        with S3Fixture() as server:
            server.state.fail_next_put("pack/", status=503)
            self.assertEqual(request(server, "PUT", "pack/a", b"a", {"if-none-match": "*"})[0], 503)
            self.assertEqual(request(server, "GET", "pack/a")[0], 404)
            server.state.fail_next_put("index", status=500, after_commit=True)
            self.assertEqual(request(server, "PUT", "index", b"b", {"if-none-match": "*"})[0], 500)
            self.assertEqual(request(server, "GET", "index")[:2], (200, b"b"))
            self.assertEqual(server.state.snapshot()["writes"], 1)
            self.assertEqual(server.state.snapshot()["injected_failures"], 2)

    def test_only_one_concurrent_writer_matches(self):
        with S3Fixture() as server:
            first = request(server, "PUT", "index", b"old", {"if-none-match": "*"})
            etag = first[2]["ETag"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request, server, "PUT", "index", value, {"if-match": etag})
                           for value in (b"left", b"right")]
                self.assertEqual(sorted(f.result()[0] for f in futures), [200, 412])


if __name__ == "__main__":
    unittest.main()
