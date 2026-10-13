# zolo-object-store

**Typed conditional object storage for Zolo, with an S3 Signature V4 backend.**

Use the `object_store` package to read and publish binary objects, coordinate
writers with opaque ETags, and stream large files. The implementation is Zolo;
cryptography, HTTP and file streams come from the standard library.

- Atomic create and compare-and-swap writes with `If-None-Match` / `If-Match`.
- S3-compatible path-style HTTP/HTTPS and AWS Signature Version 4.
- Byte arrays, owned streams and file uploads with bounded-memory multipart.
- Environment, AWS CLI and credential-process providers, with expiry-aware caching.
- A deterministic in-memory backend for application tests and typed custom callbacks.

Requires [Zolo 0.1.8-alpha](https://github.com/devzolo/zolo-releases/releases/tag/v0.1.8-alpha).
The application [zolo-gitwal](https://github.com/devzolo/zolo-gitwal) uses this library
for its durable Git WAL. This package can be used independently of that server.

## Install

Add the dependency to your `zolo.toml`, then run `zolo install`:

```toml
[dependencies]
object_store = { git = "https://github.com/devzolo/zolo-object-store.git", rev = "v0.1.1" }
```

Commit the generated `zolo.lock` to retain the resolved source and content identity.
For local development, use `object_store = { path = "../zolo-object-store" }`.

Try the self-contained [memory example](examples/memory/main.zolo), with no bucket
or credentials:

```sh
cd examples/memory
zolo install
zolo run main.zolo
```

## Read and publish an object

The bucket must already exist. Set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
and optionally `AWS_SESSION_TOKEN`; credentials are read through the provider.

```zolo
use object_store::{Store, S3Client, S3Config, CredentialProvider}

fn main() -> nil fails str {
  let client = S3Client::connect(S3Config {
    endpoint: "http://127.0.0.1:9000",
    bucket: "my-existing-bucket",
    region: "us-east-1",
    credential_provider: CredentialProvider::environment(),
  })?
  let store: Store = client.store()

  match store.put("example/payload.bin", [0, 128, 255], .Create)? {
    .Written(etag) => {
      // Keep the complete opaque token, including its quotes.
      match store.get("example/payload.bin", etag)? {
        .NotModified => print("the object has not changed"),
        _ => print("the object changed after publication"),
      }
      let replacement = store.put("example/payload.bin", [1, 128, 255], .Match(etag))?
      print(replacement)
    },
    .Conflict => print("the object already exists"),
  }
  nil
}
```

`Conflict` is an ordinary conditional-write outcome. A transport failure remains
an error: a write may have committed before its response was lost. Reconcile by
reading application state before deciding whether another write is appropriate.
The client does not automatically retry writes or follow redirects.

## API and ownership

| API | Result and ownership |
| --- | --- |
| `Store.get(key, etag = nil)` | `Found(Object)`, `Missing`, or `NotModified`; byte data and opaque ETag |
| `Store.put(key, data, condition)` | `Written(etag)` or `Conflict`; `Create` or `Match(etag)` |
| `Store.get_stream(key, etag = nil)` | `Found(StreamObject)`; caller closes its reader |
| `Store.put_stream(key, reader, length, sha256, condition)` | Consumes and closes the reader on success or failure |
| `Store.download(key, writer, max_bytes, etag = nil)` | Copies into a caller-owned writer; partial transfers are errors |
| `Store.upload_file(key, path, condition)` | Hashes and uploads an immutable file; S3 selects multipart when needed |
| `S3Client.delete(key)` | Explicit deletion of one caller-selected S3 object |
| `MemoryStore::create().store()` | Deterministic process-local test backend; copies input/output bytes |

ETags must be strong and quoted. Treat them as tokens, including multipart ETags;
do not assume they are content hashes. The S3 backend signs the payload digest,
conditional headers, host, UTC date and optional session token.

For streams, publish a downloaded temporary file only after the copy and close
succeed. Keep upload source files immutable for the complete operation. Custom
backends must perform conditional writes atomically; read-then-write is insufficient.

## Credentials and limits

Choose `CredentialProvider::environment()`, `aws_profile(profile)` or
`process(program, args)`. The process provider runs argv directly without a shell.
Use `provider.cached()` for expiry-aware refresh. Construct mutable providers and
caches inside each worker; they are not a cross-thread credential coordinator.

Single streaming PUT supports up to 5 GiB. File uploads default to multipart from
64 MiB, with 8 MiB parts; configured parts must satisfy S3's 5 MiB–5 GiB limits.
The client adjusts part size to remain within 10,000 parts. A lost multipart
completion response is still an ambiguous error. Configure incomplete-upload
lifecycle cleanup on the bucket.

MemoryStore and callback fallbacks can materialize complete objects. Use the S3
streaming backend for large-object workloads. The library does not provision buckets,
implement automatic retention, or establish a production-provider compatibility
guarantee from local fixture tests.

## Validate and contribute

```sh
zolo check src/lib.zolo
zolo test src
python -m unittest discover -s tests -p 'test_*.py' -v
```

The suites cover AWS's published SigV4 vectors, URI encoding, strong ETags,
conditional reads/writes, binary integrity, alias isolation and fixture behavior.
The [contracts and integration guide](docs/contracts.md) documents authenticated
HTTP roundtrips, streams, multipart races, credential refresh and real S3-compatible
server tests, with their limits.

Keep public callbacks typed, preserve exact bytes and report ambiguous writes as
errors. Changes to stream ownership or publication semantics should include a
regression that exercises the failure path. The library is licensed under [MIT](LICENSE).
