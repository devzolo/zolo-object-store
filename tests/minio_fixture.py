"""A real, private MinIO server owned by a single test run.

Docker is the default. A pinned source build is created locally when needed;
an executable is used only when explicitly supplied. Each run owns its container,
loopback port and disposable volume, without touching existing resources.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request


MINIO_SOURCE_COMMIT = "9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a"
DEFAULT_MINIO_IMAGE = "object-store-minio:9e49d5e7a648"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MinioFixture:
    def __init__(self, executable: Path | None, root: Path, *, image=DEFAULT_MINIO_IMAGE):
        self.executable = executable.resolve() if executable else None
        self.image = image
        self.root = root.resolve()
        self.access_key = "object-store-" + secrets.token_hex(8)
        self.secret_key = secrets.token_hex(32)
        self.url = f"http://127.0.0.1:{free_port()}" if executable else None
        self.bucket = "object-store-proof"
        self.process = None
        self.log = None
        self.container = None
        self.volume = None
        self.metadata = {"runtime": "executable" if executable else "docker"}

    @staticmethod
    def docker(*arguments, environment=None, check=True, timeout=60):
        return subprocess.run(["docker", *arguments], env=environment, check=check,
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def start_container(self, env):
        if not self.image or self.image.endswith(":latest") or (
                ":" not in self.image.rsplit("/", 1)[-1] and "@sha256:" not in self.image):
            raise ValueError("--minio-image requires a fixed tag or digest")
        inspected = self.docker("image", "inspect", self.image, check=False)
        if inspected.returncode != 0:
            if self.image == DEFAULT_MINIO_IMAGE:
                context = Path(__file__).resolve().parent / "minio"
                subprocess.run(["docker", "build", "--tag", self.image, str(context)],
                               check=True, timeout=1200,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                subprocess.run(["docker", "pull", self.image], check=True, timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            inspected = self.docker("image", "inspect", self.image)
        description = json.loads(inspected.stdout)[0]
        image_id = description["Id"]
        if self.image == DEFAULT_MINIO_IMAGE:
            revision = (description["Config"].get("Labels") or {}).get("org.opencontainers.image.revision")
            if revision != MINIO_SOURCE_COMMIT:
                raise RuntimeError("existing default MinIO image has an unexpected source revision")
        self.metadata.update(image=self.image, image_id=image_id,
                             repo_digests=description.get("RepoDigests", []))
        suffix = secrets.token_hex(12)
        name = "object-store-minio-proof-" + suffix
        self.volume = name + "-data"
        self.docker("volume", "create", "--label", "object-store.test-run=" + suffix, self.volume)
        # Keep the unique target for cleanup even if the create response is lost.
        # Never enumerate or prune other containers or volumes.
        self.container = name
        self.docker("run", "--detach", "--name", name, "--pull", "never",
                    "--label", "object-store.test-run=" + suffix,
                    "--publish", "127.0.0.1::9000", "--network", "bridge",
                    "--mount", "type=volume,source=" + self.volume + ",target=/data",
                    "--env", "MINIO_ROOT_USER", "--env", "MINIO_ROOT_PASSWORD",
                    "--env", "MINIO_BROWSER", "--env", "MINIO_UPDATE",
                    image_id, "server", "--quiet", "--address", ":9000", "/data",
                    environment=env)
        binding = self.docker("port", name, "9000/tcp").stdout.strip()
        if not binding.startswith("127.0.0.1:") or "\n" in binding:
            raise RuntimeError("MinIO container port is not isolated to loopback")
        self.url = "http://" + binding

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(MINIO_ROOT_USER=self.access_key, MINIO_ROOT_PASSWORD=self.secret_key,
                   MINIO_BROWSER="off", MINIO_UPDATE="off")
        self.log = open(self.root / "minio.log", "wb")
        try:
            if self.executable:
                self.process = subprocess.Popen(
                    [str(self.executable), "server", "--quiet", "--address",
                     self.url.removeprefix("http://"), str(self.root / "objects")],
                    env=env, stdout=self.log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                self.start_container(env)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.process and self.process.poll() is not None:
                    raise RuntimeError("isolated MinIO exited during startup")
                try:
                    with urllib.request.urlopen(self.url + "/minio/health/ready", timeout=1) as r:
                        if r.status == 200:
                            break
                except (OSError, urllib.error.HTTPError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("isolated MinIO startup timed out")
            self.request("PUT", "/" + self.bucket)
            return self
        except BaseException as error:
            try:
                self.__exit__(None, None, None)
            except Exception as cleanup_error:
                error.add_note("MinIO cleanup failed: " + str(cleanup_error))
            raise

    def request(self, method, path, body=b""):
        now = datetime.now(timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        host = self.url.removeprefix("http://")
        digest = hashlib.sha256(body).hexdigest()
        names = "host;x-amz-content-sha256;x-amz-date"
        headers = {"host": host, "x-amz-content-sha256": digest, "x-amz-date": stamp}
        canonical = (f"{method}\n{path}\n\n" + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
                     + f"\n{names}\n{digest}")
        scope = f"{day}/us-east-1/s3/aws4_request"
        signing = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        key = ("AWS4" + self.secret_key).encode()
        for part in (day, "us-east-1", "s3", "aws4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, signing.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                                    f"SignedHeaders={names}, Signature={signature}")
        req = urllib.request.Request(self.url + path, data=body if method != "GET" else None,
                                     method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()

    def environment(self):
        return dict(AWS_ACCESS_KEY_ID=self.access_key, AWS_SECRET_ACCESS_KEY=self.secret_key,
                    AWS_REGION="us-east-1")

    def capture_log(self):
        if self.container and self.log and not self.log.closed:
            result = self.docker("logs", self.container, check=False)
            self.log.write((result.stdout + result.stderr).encode())
            self.log.flush()

    def __exit__(self, *_):
        cleanup_errors = []

        def remove(kind, name, *arguments):
            try:
                result = self.docker(*arguments, check=False)
                if result.returncode and "no such " + kind not in result.stderr.lower():
                    cleanup_errors.append(kind + " " + name)
                    return False
                return True
            except (OSError, subprocess.SubprocessError) as error:
                cleanup_errors.append(kind + " " + name + ": " + type(error).__name__)
                return False

        try:
            if self.container:
                try:
                    self.capture_log()
                except (OSError, subprocess.SubprocessError) as error:
                    cleanup_errors.append("logs: " + type(error).__name__)
                if remove("container", self.container, "rm", "--force", self.container):
                    self.container = None
            if self.volume:
                if remove("volume", self.volume, "volume", "rm", self.volume):
                    self.volume = None
            if self.process is not None and self.process.poll() is None:
                try:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=10)
                except (OSError, subprocess.SubprocessError) as error:
                    cleanup_errors.append("process: " + type(error).__name__)
        finally:
            if self.log:
                self.log.close()
        if cleanup_errors:
            raise RuntimeError("MinIO cleanup failed for owned " + ", ".join(cleanup_errors))
