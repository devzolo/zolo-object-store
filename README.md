# zolo-object-store

Typed conditional object storage in pure Zolo. The package name is `object_store`.

The S3 backend uses path-style HTTP/HTTPS requests and AWS Signature Version 4.
Cryptography and binary HTTP transport come from Zolo's standard library;
there is no S3 plugin, Rust extension, or text conversion of object bytes.

This revision requires the Zolo toolchain from feature/gitwall-binary-io (commit a5f853a5 or later).
See https://github.com/devzolo/zolo-lang/tree/feature/gitwall-binary-io.

## Use

```toml
[dependencies]
object_store = { path = "../zolo-object-store" }
```

```zolo
use object_store::{S3Config, S3Client}

let config = S3Config {
  endpoint: "http://127.0.0.1:9000",
  bucket: "gitwall",
  region: "us-east-1",
  access_key: "local-access-key",
  secret_key: "local-secret-key",
  session_token: nil,
}
let store = S3Client::connect(config).unwrap().store()

match store.put("refs/main", [0, 128, 255], .Create).unwrap() {
  .Written(etag) => {
    // The token includes quotes; keep it exactly as returned.
    let unchanged = store.get("refs/main", etag).unwrap()
    let next = store.put("refs/main", [1, 128, 255], .Match(etag)).unwrap()
  },
  .Conflict => print("another writer created the object"),
}
```

The bucket must already exist. Static credentials remain supported; production
applications can select a typed credential provider instead.

## Contract

- `Store.get(key, etag = nil)` returns `Result<GetResult, str>`: `Found(Object)`,
  `Missing`, or `NotModified`. `Object` contains `data: [int]` and `etag: str`.
- `Store.put(key, data, condition)` returns `Result<PutResult, str>`:
  `Written(etag)` or `Conflict`. `PutCondition::Create` uses
  `If-None-Match: *`; `PutCondition::Match(etag)` uses `If-Match`.
- Both conditional request headers are covered by the SigV4 signature, together
  with the payload digest, UTC date, host and optional session token.
- Successful reads and writes require a quoted strong ETag from the server.
  Missing/invalid ETags are errors. ETags are opaque; they are not assumed to be MD5.
- HTTP 412 and 409 on PUT are conflicts. HTTP 404 on GET is missing. HTTP 304 is
  accepted only for a conditional GET. Other failures remain explicit errors.
- Requests use the configured timeout (300000 ms by default) and do not follow redirects or retry writes.
  A transport error can mean that the write committed but its response was lost.
  The application must resolve that outcome by rereading/reconciling its state.
- Keys are UTF-8 encoded with S3 percent encoding. Slashes and repeated slashes are
  preserved; spaces, percent signs, plus signs and non-ASCII bytes are escaped.
  Dot-only path segments (`.` and `..`) are rejected because the HTTP URL parser
  normalizes them before sending. Endpoint paths are supplied unescaped, without
  queries or fragments.
- The API does not create buckets, list/delete objects, or generate presigned URLs.
  Multipart uploads use DELETE only to abort an unfinished upload.

## Test backends

```zolo
use object_store::MemoryStore

let store = MemoryStore::create().store()
```

The in-memory backend uses deterministic increasing ETags and copies binary data
on input and output. It is process-local test state, without persistence or
cross-thread synchronization.

Custom backends implement the same typed callbacks:

```zolo
use object_store::{Store, PutCondition}

Store {
  get_object: |key: str, etag: str?| backend.get(key, etag),
  put_object: |key: str, data: [int], condition: PutCondition| backend.put(key, data, condition),
}
```

They must make each conditional write atomic. A read followed by an unconditional
write does not satisfy this contract.

## Validation

```text
zolo check src/lib.zolo
zolo test src/lib.zolo
```

The suite contains AWS's published GET and PUT signature vectors, including
canonical-request digests, plus URL encoding, conditional reads/writes, stale
ETags, invalid bytes and alias-isolation checks. The signature fixtures come from
[AWS's official SigV4 examples](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sig-v4-header-based-auth.html).

This package requires the Zolo standard-library additions `http.fetch.body_bytes`,
`ClientResponse.body_bytes`, `follow_redirects`, and byte-array SHA-256/HMAC.

## Local S3 protocol fixture

`tests/s3_fixture.py` is a Python-standard-library integration fixture that binds
only to `127.0.0.1`. It verifies each SigV4 signature, payload hash, UTC signing
window and conditional header, and serializes compare-and-swap operations under
one lock. It is not a complete S3 implementation or a substitute for testing
against the chosen production S3 provider.

```text
python tests/s3_fixture.py --port 0 --data /path/to/test-state --ready-file /path/to/ready.json
python -m unittest discover -s tests -p test_s3_fixture.py -v
```

Startup prints a JSON object containing `port`, `endpoint` and `bucket`; the same
object is written to `--ready-file` when supplied. The bucket `gitwall` already
exists. Default credentials are `gitwall-test` / `gitwall-test-secret`, region
`us-east-1`. The CLI can override the credentials, region and session token.
With no `--data`, the CLI uses a disposable temporary directory.

Python integration runners can import `S3Fixture` from the tests directory:

```python
with S3Fixture(data_dir=temporary_directory) as fixture:
    endpoint = fixture.url
    fixture.state.fail_next_put("repos/demo/packs/", status=503)
    stats = fixture.state.snapshot()
```

The process interface also exposes local test controls:

- `GET /__fixture/stats`: counters, attempted PUT keys and stored object keys.
- `POST /__fixture/control`: JSON with `operation: "fail_next_put"`, `prefix`,
  and optional `status` (default 503), `count`, `after_commit` and `disconnect`.
  An injected failure can occur before storage or after storage to test an
  ambiguous response. These endpoints are test-only and require no signature.

### Zolo HTTP roundtrip

Run the complete client against the authenticated fixture using the current Zolo
toolchain:

```text
cd tests
zolo install
cd ..
python tests/s3_roundtrip.py --zolo /path/to/zolo
```

The runner checks arbitrary bytes (including NUL, 128 and 255), Unicode keys,
session-token signing, empty objects, conditional GET, create/update conflicts,
HTTP 409, invalid bytes and errors before/after commit. Fixture counters assert
that writes are not automatically retried.

For deterministic races, an imported fixture exposes
`state.before_put = callback`. The callback receives `(key, body, headers)`
after signature verification and before the storage lock/CAS; it may coordinate
test threads with a barrier without blocking independent writes.

The sibling `zolo-gitwall/tests/compaction_thin_tail.py` regression exercises the host application.
It constructs a real Git REF_DELTA pack against an unreachable historical object,
forces a concurrent append between compaction and its CAS, and verifies that a
cold third node can clone the result. Run `--proof-only` to validate the adversarial
pack with stock Git alone, or pass `--zolo /path/to/zolo` for the complete race.

## Streams and files

    use std::stream
    use object_store::{S3Config, S3Client, CredentialProvider}

    let store = S3Client::connect(S3Config {
      endpoint: "https://s3.us-east-1.amazonaws.com",
      bucket: "my-existing-bucket",
      region: "us-east-1",
      credential_provider: CredentialProvider::environment(),
    }).unwrap().store()

    let uploaded = store.upload_file("packs/new.pack", "/spool/new.pack", .Create).unwrap()
    let writer = stream.create("/cache/new.pack.partial").unwrap()
    let downloaded = store.download("packs/new.pack", writer, 1073741824).unwrap()
    writer.close().unwrap()

The upload_file(key, path, condition) method hashes and uploads an open file
with bounded memory. The caller must keep the source immutable throughout the
operation. The download(key, writer, max_bytes, etag = nil) method returns
Found(Download) with etag and bytes_written, Missing, or NotModified. The writer
belongs to the caller; a partial download is an error, and the temporary
destination must only be published after success.

For pipelines, get_stream(key, etag = nil) returns Found(StreamObject) with
body: Reader, etag, and content_length: int?. The caller owns and closes the
reader. The put_stream(key, reader, content_length, sha256, condition) method
consumes and closes its reader, including on failure. SHA-256 must be the exact
lowercase digest of the bytes to be sent; content length must match. S3 validates
the signed payload hash and HTTP detects source failures and length mismatches.
A single streaming PUT supports at most 5 GiB; use upload_file for larger objects.

Store retains its byte-array callbacks. Optional get_stream_object,
put_stream_object, and upload_file_object callbacks provide native streaming
backends. S3 implements all three. Legacy/custom callbacks and MemoryStore
adapt through byte arrays; they intentionally retain their existing in-memory
behavior and are not suitable for bounded-memory storage of large objects.

### Multipart

S3Config.multipart_threshold defaults to 64 MiB and can be lowered to 1 byte
for tests. The multipart_part_size defaults to 8 MiB and must be 5 MiB..5 GiB.
The client increases the effective part size when necessary to stay within
10,000 parts. Each part is read, hashed and uploaded through bounded views of
the file; the complete object is never assembled in memory.

Initiation and part uploads do not publish an object. The original Create or
Match(etag) condition is signed on CompleteMultipartUpload; HTTP 409/412 remain
Conflict. Completion requires a success XML root and a quoted ETag: HTTP 200
containing an XML Error is an error. Failed parts, failed completion and
conflicts trigger an abort. Cleanup failure remains visible in the returned
error. A lost completion response is still an ambiguous error even if the
object became visible; the client never retries a write automatically.

These semantics follow AWS's
[conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[completion response contract](https://docs.aws.amazon.com/AmazonS3/latest/API/API_CompleteMultipartUpload.html)
and [multipart limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/qfacts.html).
If initiation commits but its response is lost, the client has no upload ID to
abort; the bucket's incomplete-upload lifecycle policy controls that orphan.

## Credential providers

Credentials contains access_key, secret_key, session_token: str? and
expires_at: float? (Unix seconds). CredentialProvider wraps a typed
load: fn() -> Result<Credentials, str> callback and validates credentials
before each signature. Choose S3Config.credential_provider or the legacy
static fields; specifying both is an error.

- CredentialProvider::environment() reads AWS_ACCESS_KEY_ID,
  AWS_SECRET_ACCESS_KEY and optional AWS_SESSION_TOKEN on each resolution.
- CredentialProvider::process(program, args, timeout_ms = 30000) executes an
  argv vector directly, without a shell. It accepts AWS credential-process JSON
  version 1, with optional session token and RFC3339 expiration. Output is
  limited to 64 KiB; process errors do not include stdout, stderr or secrets.
- CredentialProvider::aws_profile(profile = nil, program = "aws") delegates
  to AWS CLI configure export-credentials --format process and caches the
  result. AWS CLI handles its configured profile, SSO or role chain; the library
  neither installs the CLI nor initiates an interactive login.
- provider.cached(refresh_before_seconds = 300, clock = || os.time())
  refreshes before expiration. The clock is injectable for deterministic tests.
  Refresh failures are errors; stale credentials are not silently reused.
  Credentials without expiration are cached indefinitely, matching AWS's
  credential-process protocol. Use the uncached environment provider for
  externally rotated environment credentials.

Providers and their caches are local to one runtime/worker. Applications should
construct them inside each worker. An expired provider response fails before
any signed HTTP request is sent. No credential values are written to logs.

The external protocol is documented by
[AWS credential_process](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html)
and [AWS CLI export-credentials](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html).

## Streaming regression suite

    python tests/s3_streaming.py --zolo /path/to/zolo
    python tests/s3_streaming.py --zolo /path/to/zolo --emit native
    python tests/s3_streaming.py --zolo /path/to/zolo --emit llvm

The authenticated multipart fixture checks reserved characters in signed query
parameters, a 12 MiB binary file, conditional completion, failed parts, abort,
HTTP-200 error XML, a lost response after commit, a truncated download, and a
real credential subprocess. The multipart_fixture.py server is a fault fixture,
not an S3 compatibility certification. The main Gitwall integration also runs
against MinIO where available.

The current APIs require std::stream, streaming HTTP and hashes,
datetime.parse_rfc3339, and bounded xml.parse_document from the matching Zolo
toolchain. The XML parser rejects DTD/external entities and caps document size
and nesting depth; the date parser checks complete input and UTC offsets.
## Real MinIO and client memory proof

`tests/s3_minio_streaming.py` starts MinIO in Docker by default, with temporary
credentials, an empty disposable bucket, a unique container and volume, and a
random port bound only to `127.0.0.1`. The fixture builds the official MinIO source
commit `9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a` (release
`RELEASE.2025-10-15T17-29-55Z`) when the default local image is absent. Build tools
and the server stay inside Docker; the Go and Debian base images are pinned by
digest in the sibling Gitwall `tests/minio/Dockerfile`.

```text
python tests/s3_minio_streaming.py --zolo /path/to/zolo --emit vm --report tests/reports/minio-docker-vm.json
python tests/s3_minio_streaming.py --zolo /path/to/zolo --emit native --report tests/reports/minio-docker-native.json
python tests/s3_minio_streaming.py --zolo /path/to/zolo --emit llvm --report tests/reports/minio-docker-llvm.json
```

Use `--minio-image repository:fixed-tag` or `repository@sha256:digest` to select
a different image. The fixture resolves it to an immutable image ID before
starting the server and records that identity in its report. `--minio /path/to/minio`
is an explicit opt-in to a local executable; there is no automatic host fallback.
Only this run's container and volume are removed on success or failure. Images
and Docker build caches are retained for reuse; existing resources are untouched.

The default 128 MiB random file is generated and verified in chunks. The proof
checks single PUT and multipart uploads, create and ETag-match CAS (including
conflicts), conditional reads, full download lengths, and SHA-256 digests.
It samples only the Zolo client process after warmup; Python, compilation and
the separate MinIO process are excluded. The default transfer RSS growth bound
is 64 MiB, configurable with `--max-rss-growth-mib`. JSON reports record the
actual baseline, peak, sample count, bound, checks, `storage_mode` and immutable
MinIO image ID. All child processes and
temporary data are cleaned up even on failure.

The runner reuses the sibling Gitwall `tests/minio_fixture.py`; override
`--fixture-dir` when the repositories are in another layout. No cloud account,
existing bucket, global installation or interactive AWS login is used. The
streaming fixture also builds `fake_aws_cli.zolo` to test the AWS CLI credential
export argv and protocol against a real local subprocess.

The Windows validation run on 2026-09-26 passed authenticated roundtrips and the
multipart/credential subprocess fixture in VM, native and LLVM. The Docker
MinIO proof passed in all three backends with a 128 MiB payload:

| Backend | Client RSS growth after warmup | Bound |
| --- | ---: | ---: |
| VM | 5.89 MiB | 64 MiB |
| Native | 3.53 MiB | 64 MiB |
| LLVM | 3.00 MiB | 64 MiB |

The JSON reports are `tests/reports/minio-docker-vm.json`,
`minio-docker-native.json` and `minio-docker-llvm.json`. Each records
`storage_mode: docker` and image identity
`sha256:f64e8b523ee6bcd3acf0abeaa72e0fd5b0b7c5be191d69fd6aa6c19bd8e9307c`.
The fixture smoke also verified signed binary PUT/GET and removal of its owned
container and volume after normal completion and an injected workload/log failure.
These are local measurements; the runner records actual values on each invocation.
Earlier local-executable reports remain under `minio-streaming-*.json`.

Large uploads negotiate `Expect: 100-continue` before reading their source, so
a real server can reject authentication, limits or CAS from the headers. A
received rejection retains its HTTP status even when no body was consumed;
transport errors never become CAS conflicts. Success requires exact upload
consumption. Streaming write requests do not follow redirects or automatically
retry an ambiguous commit. The S3 timeout defaults to 300,000 ms, and file/hash
copies use bounded chunks; response size and declared lengths remain checked.
