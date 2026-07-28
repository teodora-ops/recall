"""
bedrock_check.py — is Bedrock reachable, and which model IDs work?

Tries a first-party Amazon chat model and a Titan embedding model, and prints
exactly what comes back for each, so that a change in the error message is
visible between runs. Useful when model access has just been requested, since
enablement is not always instant and the failure mode shifts as it lands.
 
Usage:  python bedrock_check.py
"""
 
import os
import sys
 
try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("boto3 not installed. Run:  pip install boto3 python-dotenv")
 
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("(python-dotenv not installed — relying on shell env vars)\n")
 
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-west-2"
 
KEY = os.getenv("AWS_ACCESS_KEY_ID")
SECRET = os.getenv("AWS_SECRET_ACCESS_KEY")
if not KEY or not SECRET:
    sys.exit("No AWS keys found in .env or environment "
             "(need AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY).")
 
print(f"Region:     {REGION}")
print(f"Access key: {KEY[:4]}...{KEY[-4:]}")
print("-" * 60)
 
session = boto3.Session(
    aws_access_key_id=KEY,
    aws_secret_access_key=SECRET,
    region_name=REGION,
)
rt = session.client("bedrock-runtime")
 
# Chat models, tried in order. Most Bedrock documentation assumes the eu.*
# cross-region inference profile form, so it is tried first — but on this
# account it returns "ValidationException: The provided model identifier is
# invalid" and the bare on-demand id is what actually works. Do not copy the
# profile form from the docs without checking it here first.
CHAT_MODELS = [
    "eu.amazon.nova-pro-v1:0",
    "amazon.nova-pro-v1:0",
    "eu.amazon.nova-lite-v1:0",
]
 
EMBED_MODELS = [
    "amazon.titan-embed-text-v2:0",
    "amazon.titan-embed-text-v1",
]
 
 
def try_chat(model_id):
    resp = rt.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": "Reply with the single word: ok"}]}],
        inferenceConfig={"maxTokens": 8, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()
 
 
def try_embed(model_id):
    import json
    resp = rt.invoke_model(
        modelId=model_id,
        body=json.dumps({"inputText": "hello"}),
    )
    vec = json.loads(resp["body"].read())["embedding"]
    return f"{len(vec)} dimensions"
 
 
def run(label, model_id, fn):
    try:
        result = fn(model_id)
        print(f"[ OK ]   {label:<32} {model_id}")
        print(f"         -> {result}")
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        print(f"[FAIL]   {label:<32} {model_id}")
        print(f"         -> {code}: {msg}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL]   {label:<32} {model_id}")
        print(f"         -> {type(e).__name__}: {e}")
        return False
 
 
chat_ok = False
for m in CHAT_MODELS:
    if run("chat", m, try_chat):
        chat_ok = True
        break
 
print("-" * 60)
 
embed_ok = False
for m in EMBED_MODELS:
    if run("embeddings", m, try_embed):
        embed_ok = True
        break
 
print("-" * 60)
if chat_ok and embed_ok:
    print("UNBLOCKED. Record the working model ids in .env and carry on.")
elif chat_ok or embed_ok:
    print("PARTIAL. One of the two works, so access is not blocked wholesale;")
    print("what remains is per-model and narrower. Check the failing error.")
else:
    print("NEITHER WORKS. If the error is unchanged between runs, access has")
    print("not landed yet. If it has changed, that is progress — read it.")