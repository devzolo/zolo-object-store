"""Exercise bounded S3 streams, multipart CAS and ambiguous failures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from multipart_fixture import MultipartFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zolo", default=os.environ.get("ZOLO_BIN", "zolo"))
    parser.add_argument("--emit", choices=("vm", "native", "llvm"), default="vm")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="object-store-streams-") as temporary:
        temporary = Path(temporary)
        source = temporary / "source.pack"
        with source.open("wb") as output:
            chunk = bytes(range(256)) * 256
            for _ in range(192):
                output.write(chunk)
            output.write(b"\0\xff\x80end")
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        helper = temporary / "credential_provider.py"
        helper.write_text(
            "import json,os\nfrom datetime import datetime,timedelta,timezone\n"
            "print(json.dumps({'Version':1,'AccessKeyId':os.environ['AWS_ACCESS_KEY_ID'],"
            "'SecretAccessKey':os.environ['AWS_SECRET_ACCESS_KEY'],"
            "'SessionToken':os.environ['AWS_SESSION_TOKEN'],"
            "'Expiration':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}))\n",
            encoding="utf-8")
        with MultipartFixture(session_token="temporary-session") as fixture:
            fixture.state.fail_next_put("part-failure/", status=503)
            env = dict(os.environ, OBJECT_STORE_TEST_ENDPOINT=fixture.url,
                       OBJECT_STORE_TEST_FILE=str(source),
                       OBJECT_STORE_TEST_DOWNLOAD=str(temporary / "download.pack"),
                       OBJECT_STORE_TEST_PYTHON=sys.executable,
                       OBJECT_STORE_TEST_CREDENTIAL_HELPER=str(helper),
                       AWS_ACCESS_KEY_ID="gitwall-test", AWS_SECRET_ACCESS_KEY="gitwall-test-secret",
                       AWS_SESSION_TOKEN="temporary-session")
            aws_cli = temporary / ("fake-aws-cli" + (".exe" if os.name == "nt" else ""))
            subprocess.run([args.zolo, "build", "fake_aws_cli.zolo", "--emit", "native",
                            "--debug", "-o", str(aws_cli)], cwd=directory, env=env,
                           check=True, timeout=180)
            env["OBJECT_STORE_TEST_AWS_CLI"] = str(aws_cli)
            command = [args.zolo, "run", "s3_streaming.zolo"]
            if args.emit != "vm":
                binary = temporary / ("streaming-" + args.emit + (".exe" if os.name == "nt" else ""))
                subprocess.run([args.zolo, "build", "s3_streaming.zolo", "--emit", args.emit,
                                "--debug", "-o", str(binary)], cwd=directory, env=env, check=True, timeout=180)
                command = [str(binary)]
            try:
                subprocess.run(command, cwd=directory, env=env, check=True, timeout=180)
            finally:
                print(json.dumps(fixture.state.snapshot(), indent=2))
            state = fixture.state.snapshot()
            assert state["multipart_started"] == 7, state
            assert state["put_keys"].count("profile/object") == 1, state
            assert state["multipart_completed"] == 3, state
            assert state["multipart_complete_calls"] == 6, state
            assert state["multipart_abort_calls"] == 5, state
            assert state["injected_failures"] == 1, state
            assert not fixture.state.uploads, fixture.state.uploads
            assert "embedded-error/pack" not in fixture.state.objects
            assert "part-failure/pack" not in fixture.state.objects
            assert hashlib.sha256(fixture.state.objects["multipart/space +%/café"].body).hexdigest() == expected
            assert hashlib.sha256((temporary / "download.pack").read_bytes()).hexdigest() == expected
            assert not fixture.state.signature_errors, fixture.state.signature_errors
            print("All streaming, multipart and failure assertions passed.")


if __name__ == "__main__":
    main()