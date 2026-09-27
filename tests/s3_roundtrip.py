"""Run the Zolo S3 client against an authenticated local HTTP fixture."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from s3_fixture import S3Fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zolo", default=os.environ.get("ZOLO_BIN", "zolo"))
    parser.add_argument("--emit", choices=("vm", "native", "llvm"), default="vm")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="object-store-roundtrip-") as data:
        with S3Fixture(data_dir=data, session_token="temporary-session") as fixture:
            fixture.state.fail_next_put("transient/", status=503)
            fixture.state.fail_next_put("uncertain/", status=500, after_commit=True)
            fixture.state.fail_next_put("conflict/", status=409)
            env = dict(os.environ, OBJECT_STORE_TEST_ENDPOINT=fixture.url,
                       OBJECT_STORE_TEST_SESSION="temporary-session")
            command = [args.zolo, "run", "s3_roundtrip.zolo"]
            if args.emit != "vm":
                binary = Path(data) / ("roundtrip-" + args.emit + (".exe" if os.name == "nt" else ""))
                subprocess.run([args.zolo, "build", "s3_roundtrip.zolo", "--emit", args.emit,
                                "--debug", "-o", str(binary)], cwd=directory, env=env,
                               check=True, timeout=180)
                command = [str(binary)]
            try:
                subprocess.run(command, cwd=directory, env=env, check=True, timeout=60)
            except subprocess.CalledProcessError:
                print("S3 fixture diagnostics:", fixture.state.snapshot())
                raise
            snapshot = fixture.state.snapshot()
            assert snapshot["rejected_signatures"] == 0, snapshot
            assert snapshot["writes"] == 3, snapshot
            assert snapshot["conflicts"] == 2, snapshot
            assert snapshot["injected_failures"] == 3, snapshot
            assert snapshot["put_keys"].count("transient/a") == 1, snapshot
            assert snapshot["put_keys"].count("uncertain/a") == 1, snapshot
            print("Authenticated HTTP, binary roundtrip, CAS and no automatic write retries: passed")


if __name__ == "__main__":
    main()
