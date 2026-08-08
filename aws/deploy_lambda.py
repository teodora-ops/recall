"""
deploy_lambda.py — package the agent turn and ship it to Lambda.

    python aws/deploy_lambda.py --build          just build the zip
    python aws/deploy_lambda.py --deploy         build and upload
    python aws/deploy_lambda.py --invoke <url>   call the Function URL

Two things make this awkward enough to be worth automating:

1. **Wheels must match Lambda's platform, not yours.** psycopg[binary] ships
   compiled wheels; installing on Windows gives you win_amd64 wheels that
   Lambda cannot load. pip is told explicitly to fetch manylinux wheels for
   the runtime's Python version, which works without Docker.

2. **The CA has to travel with the code.** db.py looks for certs/root.crt
   relative to itself, so the certificate is packaged alongside the modules
   and the connection verifies inside Lambda exactly as it does locally.

The function itself is created once in the console — deliberately. Doing it
from here would need iam:CreateRole and iam:PassRole on the machine user, which
is far more standing privilege than a hackathon key should carry.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build" / "lambda"
ZIP = ROOT / ".build" / "recall-agent-turn.zip"

FUNCTION = os.getenv("LAMBDA_FUNCTION_NAME", "recall-agent-turn")
RUNTIME_PY = "3.12"

# Everything the agent turn actually imports. Kept explicit rather than
# globbing the repo: the demo scripts, the seeder and the checks have no place
# in a production artifact.
MODULES = ["db.py", "embeddings.py", "recall.py", "txn.py", "agent.py",
           "sandbox.py", "persona.py"]

# boto3 is already in the Lambda runtime, so it is not packaged.
#
# typing_extensions is listed EXPLICITLY, and that is not belt-and-braces.
# psycopg declares it conditionally:
#
#     typing-extensions>=4.6; python_version < "3.13"
#     tzdata;                 sys_platform == "win32"
#
# --platform and --python-version decide which *wheels* are compatible; they do
# not fully re-evaluate environment markers, so pip resolves those against the
# BUILD machine. Building on Windows/3.14 therefore skipped typing_extensions
# (3.14 >= 3.13) and packaged tzdata (win32) — precisely inverted for a
# Linux/3.12 target. The function imported cleanly and then died at runtime
# with ModuleNotFoundError: No module named 'typing_extensions'.
DEPS = [
    "psycopg[binary]==3.3.4",
    "typing_extensions>=4.6",
]


def build() -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    for m in MODULES:
        shutil.copy2(ROOT / m, BUILD / m)
    shutil.copy2(ROOT / "aws" / "handler.py", BUILD / "handler.py")

    certs = BUILD / "certs"
    certs.mkdir()
    shutil.copy2(ROOT / "certs" / "root.crt", certs / "root.crt")

    print(f"packaging deps for linux/py{RUNTIME_PY} …")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", *DEPS,
         "--target", str(BUILD),
         "--platform", "manylinux2014_x86_64",
         "--python-version", RUNTIME_PY,
         "--only-binary=:all:", "--quiet", "--upgrade"],
        check=True,
    )

    ZIP.parent.mkdir(parents=True, exist_ok=True)
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in BUILD.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(BUILD))

    mb = ZIP.stat().st_size / 1024 / 1024
    print(f"built {ZIP.name}  ({mb:.1f} MB)")
    if mb > 50:
        print("WARNING: over 50 MB — direct upload will fail, use S3")
    return ZIP


def client():
    import boto3
    from dotenv import load_dotenv
    load_dotenv()
    return boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "eu-west-2"),
    ).client("lambda")


def deploy() -> None:
    from botocore.exceptions import ClientError
    zip_path = build()
    lam = client()

    try:
        info = lam.get_function(FunctionName=FUNCTION)
        cfg = info["Configuration"]
        print(f"function   : {cfg['FunctionName']} ({cfg['Runtime']}, "
              f"{cfg['MemorySize']} MB, {cfg['Timeout']}s timeout)")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "AccessDeniedException"):
            sys.exit(
                f"cannot see function {FUNCTION!r}: {code}\n"
                "Create it in the console first (Python 3.12, default role, "
                "Function URL), then grant the machine user lambda:GetFunction, "
                "lambda:UpdateFunctionCode and lambda:UpdateFunctionConfiguration."
            )
        raise

    print("uploading …")
    r = lam.update_function_code(FunctionName=FUNCTION,
                                 ZipFile=zip_path.read_bytes(), Publish=True)
    print(f"uploaded   : version {r['Version']}, {r['CodeSize']} bytes")

    # The handler lives at handler.lambda_handler and needs long enough for a
    # cold start plus a serializable transaction. Retried, because Lambda
    # rejects a config change while the code upload is still settling
    # (ResourceConflictException) — which it always is, immediately after one.
    import time
    for attempt in range(1, 7):
        try:
            lam.update_function_configuration(
                FunctionName=FUNCTION, Handler="handler.lambda_handler",
                Timeout=30, MemorySize=512)
            print("config     : handler.lambda_handler, 30s, 512 MB")
            break
        except ClientError as e:
            if attempt == 6:
                print(f"config     : NOT updated ({e.response['Error']['Code']}) "
                      f"— set it in the console")
                break
            time.sleep(6)

    print("\nRemaining, in the console: set COCKROACH_URL on the function's "
          "environment variables (a credential that can write).")


def invoke(url: str) -> None:
    import urllib.request
    for method in ("GET", "POST"):
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read())
            print(f"{method:<5} {r.status}  "
                  f"{json.dumps({k: body[k] for k in list(body)[:7]}, default=str)[:220]}")
        except Exception as e:  # noqa: BLE001
            print(f"{method:<5} FAILED  {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--deploy", action="store_true")
    ap.add_argument("--invoke", metavar="FUNCTION_URL")
    a = ap.parse_args()
    if a.invoke:
        invoke(a.invoke)
    elif a.deploy:
        deploy()
    elif a.build:
        build()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
