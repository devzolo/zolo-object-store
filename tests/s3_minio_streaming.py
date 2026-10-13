"""Exercise 128 MiB streaming against an isolated real MinIO and measure client RSS.

The server fixture and pinned source build are distributed with this library.
The server, compiler and Python memory are excluded from client transfer RSS.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

def rss(pid):
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            return 0
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize
            return 0
        finally:
            kernel.CloseHandle(handle)
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zolo", type=Path, required=True)
    parser.add_argument("--minio", type=Path, help="explicit local executable; default: Docker")
    parser.add_argument("--minio-image", default="object-store-minio:9e49d5e7a648", help="fixed Docker tag or digest")
    parser.add_argument("--emit", choices=("vm", "native", "llvm"), default="vm")
    parser.add_argument("--megabytes", type=int, default=128)
    parser.add_argument("--max-rss-growth-mib", type=int, default=64)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fixture-dir", type=Path,
        default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    assert 64 <= args.megabytes <= 256
    sys.path.insert(0, str(args.fixture_dir.resolve()))
    from minio_fixture import MinioFixture
    directory = Path(__file__).resolve().parent
    report = {"backend": args.emit, "payload_bytes": args.megabytes * 1048576}
    with tempfile.TemporaryDirectory(prefix="object-store-minio-") as temporary:
        root = Path(temporary)
        source = root / "source.pack"
        digest = hashlib.sha256()
        with source.open("wb") as file:
            for _ in range(args.megabytes):
                chunk = os.urandom(1048576)
                digest.update(chunk)
                file.write(chunk)
        with MinioFixture(args.minio, root / "minio", image=args.minio_image) as storage:
            report["storage"] = storage.metadata
            report["storage_mode"] = storage.metadata["runtime"]
            report["minio_image_id"] = storage.metadata.get("image_id")
            env = os.environ.copy()
            env.pop("AWS_SESSION_TOKEN", None)
            env.update(storage.environment(), OBJECT_STORE_TEST_ENDPOINT=storage.url,
                OBJECT_STORE_TEST_BUCKET=storage.bucket, OBJECT_STORE_TEST_FILE=str(source),
                OBJECT_STORE_TEST_DOWNLOAD=str(root / "download.pack"),
                OBJECT_STORE_TEST_GATE=str(root / "transfer.ready"))
            command = [str(args.zolo.resolve()), "run", "s3_minio_streaming.zolo", "--no-cache"]
            if args.emit != "vm":
                binary = root / ("client-" + args.emit + (".exe" if os.name == "nt" else ""))
                subprocess.run([str(args.zolo.resolve()), "build", "s3_minio_streaming.zolo",
                    "--emit", args.emit, "--debug", "-o", str(binary)],
                    cwd=directory, env=env, check=True, timeout=240)
                command = [str(binary)]
            process = None
            reader = None
            samples = []
            baseline = []
            output = []
            ready = threading.Event()
            stopped = threading.Event()
            try:
                process = subprocess.Popen(command, cwd=directory, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                def read_lines():
                    for line in process.stdout:
                        output.append(line.rstrip())
                        if line.strip() == "RSS_BASELINE":
                            baseline.append(rss(process.pid))
                            ready.set()
                reader = threading.Thread(target=read_lines, daemon=True)
                reader.start()
                deadline = time.monotonic() + 60
                while not ready.wait(0.05):
                    if process.poll() is not None:
                        raise AssertionError("\n".join(output))
                    if time.monotonic() > deadline:
                        raise TimeoutError("client startup/baseline timed out")
                assert baseline[0] > 0, "RSS sampling unavailable"
                def sample():
                    while not stopped.wait(0.02):
                        samples.append(rss(process.pid))
                sampler = threading.Thread(target=sample, daemon=True)
                sampler.start()
                (root / "transfer.ready").touch()
                status = process.wait(timeout=300)
                stopped.set()
                sampler.join(timeout=5)
                reader.join(timeout=5)
                assert status == 0, "\n".join(output)
                for path in (root / "download.pack", root / "download.pack.single"):
                    assert path.stat().st_size == report["payload_bytes"]
                    with path.open("rb") as file:
                        assert hashlib.file_digest(file, "sha256").digest() == digest.digest()
                report.update(source_sha256=digest.hexdigest(), baseline_rss_bytes=baseline[0],
                    peak_rss_bytes=max(samples + baseline), rss_samples=len(samples),
                    client_output=output, checks=["single PUT and multipart with real MinIO",
                    "create/match/stale/create-conflict CAS", "conditional GET and missing object",
                    "binary length and streaming SHA-256 verified twice"])
                report["rss_growth_bytes"] = max(0, report["peak_rss_bytes"] - baseline[0])
                report["max_rss_growth_bytes"] = args.max_rss_growth_mib * 1048576
                report["rss_within_bound"] = report["rss_growth_bytes"] <= report["max_rss_growth_bytes"]
                if args.report:
                    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                print(json.dumps(report, indent=2), flush=True)
                assert report["rss_within_bound"], "client transfer RSS exceeded configured bound"
            except BaseException:
                storage.capture_log()
                report.update(client_output=output, minio_log=(root / "minio" / "minio.log").read_text(errors="replace")[-12000:])
                print(json.dumps(report, indent=2), flush=True)
                if args.report:
                    args.report.resolve().parent.mkdir(parents=True, exist_ok=True)
                    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                raise
            finally:
                stopped.set()
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                if process and process.stdout:
                    process.stdout.close()
                if reader:
                    reader.join(timeout=5)

if __name__ == "__main__":
    main()
