"""CLI for the Bedrock KB anchor benchmark's per-arm AWS lifecycle.

Wraps the CDK CLI (deploy/destroy) and the boto3 helpers in
scripts/_bedrock_kb.py (upload/ingest/status) behind one entry point, keyed
by arm id from benchmark_data/bedrock_kb/config.json. Resource ids
(knowledge base, data source, bucket) are never hardcoded here: they are
always resolved fresh from the deployed stack's CloudFormation outputs via
stack_outputs(), so this file has nothing to go stale.

    uv run python scripts/bedrock_kb_stack.py deploy  --arm B0-default-v1
    uv run python scripts/bedrock_kb_stack.py upload  --arm B0-default-v1
    uv run python scripts/bedrock_kb_stack.py ingest  --arm B0-default-v1
    uv run python scripts/bedrock_kb_stack.py status  --arm B0-default-v1
    uv run python scripts/bedrock_kb_stack.py destroy --arm B0-default-v1

Arm "P" (pdf_corpus_search) has no AWS stack and is rejected by every
subcommand here; see benchmark_bedrock_kb.py for that arm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from _bedrock_kb import (  # noqa: E402
    TAG_KEY,
    ingest,
    ingest_stamp_matches,
    load_state,
    make_ingest_stamp,
    save_state,
    stack_name,
    stack_outputs,
    upload,
    wait_ingest,
)
from benchmark_bedrock_kb import check_corpus_quota  # noqa: E402

CONFIG_PATH = REPO / "benchmark_data" / "bedrock_kb" / "config.json"
STATE_PATH = REPO / "benchmark_data" / "bedrock_kb" / ".stack.json"
MANIFEST_PATH = REPO / "benchmark_data" / "corpus_search" / "manifest.json"
INFRA_DIR = REPO / "infra" / "bedrock_kb"


def sha256_json(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _arm_config(cfg: dict, arm_id: str) -> dict:
    if arm_id == "P":
        raise SystemExit("arm P has no AWS stack; nothing for this CLI to do")
    try:
        return cfg["arms"][arm_id]
    except KeyError:
        raise SystemExit(f"unknown arm id {arm_id!r}; see {CONFIG_PATH}")


def _session(cfg: dict):
    return boto3.Session(region_name=cfg["region"])


def _cdk_env() -> dict:
    env = dict(os.environ)
    # node's tested-version check otherwise warns/fails on newer node; this
    # is a CDK CLI concern, not an AWS credential, so it is safe to force.
    env["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] = "1"
    return env


def _cdk(args: list[str]) -> None:
    """Run the CDK CLI against infra/bedrock_kb/app.py.

    cdk.json's default app command ("python app.py") resolves "python"
    against PATH, which is not guaranteed to be an interpreter with
    aws_cdk installed. Passing --app explicitly pins the app to the
    interpreter this script itself is running under (the project venv).
    """
    cmd = [
        "npx",
        "aws-cdk@2",
        *args,
        "--app",
        f"{sys.executable} app.py",
    ]
    subprocess.run(cmd, cwd=INFRA_DIR, env=_cdk_env(), check=True)


def _empty_bucket(s3, bucket: str) -> int:
    """Delete every object in bucket. Returns the number deleted.

    auto_delete_objects is deliberately not set on the CDK bucket (it would
    add a Lambda-backed custom resource), so RemovalPolicy.DESTROY alone
    cannot remove a bucket that still holds the 100 uploaded PDFs. This
    must run before cdk destroy.
    """
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket):
        keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        if batch:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
    return len(keys)


def _wait_delete_complete(
    cfn, name: str, poll_s: float = 10, max_wait_s: float = 900
) -> None:
    """Poll until the stack is gone or reports DELETE_COMPLETE.

    A partially failed destroy leaves the stack's explicit resource names
    (role name, bucket names) stranded, which blocks redeploying the same
    arm id. Confirming completion here, rather than trusting that
    `cdk destroy` merely returned, is what makes that failure loud instead
    of silent.
    """
    start = time.monotonic()
    while True:
        out = stack_outputs(cfn, name)
        if not out or out.get("status") == "DELETE_COMPLETE":
            return
        status = out.get("status", "")
        if status.endswith("FAILED"):
            raise RuntimeError(f"destroy of {name} failed: status {status}")
        if time.monotonic() - start >= max_wait_s:
            raise TimeoutError(
                f"{name} did not reach DELETE_COMPLETE within {max_wait_s}s "
                f"(status={status!r})"
            )
        time.sleep(poll_s)


def cmd_deploy(arm_id: str) -> int:
    cfg = load_config()
    acfg = _arm_config(cfg, arm_id)
    config_sha = sha256_json(acfg)
    name = stack_name(arm_id)

    cfn = _session(cfg).client("cloudformation")
    out = stack_outputs(cfn, name)
    if out:
        existing = out.get("tags", {}).get(TAG_KEY)
        if existing is not None and existing != config_sha:
            print(
                f"REFUSING to deploy {arm_id}: stack {name} already exists "
                f"tagged {TAG_KEY}={existing!r}, but the current config in "
                f"{CONFIG_PATH} hashes to {config_sha!r}. Indexes are "
                "immutable: add a new arm id with a bumped -vN suffix and "
                "deploy that instead of editing this one in place."
            )
            return 2

    _cdk(["deploy", name, "--require-approval", "never"])
    print(f"{arm_id}: deployed {name}")
    return 0


def cmd_upload(arm_id: str) -> int:
    cfg = load_config()
    _arm_config(cfg, arm_id)
    name = stack_name(arm_id)

    sess = _session(cfg)
    cfn, s3 = sess.client("cloudformation"), sess.client("s3")
    out = stack_outputs(cfn, name)
    if not out:
        print(f"ERROR {arm_id}: stack {name} not deployed")
        return 2

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    errors = check_corpus_quota(manifest, REPO)
    if errors:
        for e in errors:
            print("QUOTA", e)
        return 2

    files = [REPO / d["path"] for d in manifest["docs"]]
    n = upload(s3, out["SourceBucketName"], files, REPO)
    print(f"{arm_id}: uploaded and verified {n} objects")
    return 0


def cmd_ingest(arm_id: str) -> int:
    cfg = load_config()
    _arm_config(cfg, arm_id)
    name = stack_name(arm_id)

    sess = _session(cfg)
    cfn, agent = sess.client("cloudformation"), sess.client("bedrock-agent")
    out = stack_outputs(cfn, name)
    if not out:
        print(f"ERROR {arm_id}: stack {name} not deployed")
        return 2

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = len(manifest["docs"])

    job_id = ingest(agent, out["KnowledgeBaseId"], out["DataSourceId"])
    job = wait_ingest(
        agent, out["KnowledgeBaseId"], out["DataSourceId"], job_id, poll_s=20
    )
    stats = job.get("statistics", {})
    print(f"{arm_id}: ingest {job['status']} {json.dumps(stats)}")
    if job["status"] != "COMPLETE":
        print(f"{arm_id}: failureReasons={job.get('failureReasons')}")
        return 1

    scanned = stats.get("numberOfDocumentsScanned", 0)
    indexed = stats.get("numberOfNewDocumentsIndexed", 0) + stats.get(
        "numberOfModifiedDocumentsIndexed", 0
    )
    failed = stats.get("numberOfDocumentsFailed", 0)
    if scanned != expected or indexed != expected or failed != 0:
        print(
            f"ERROR {arm_id}: ingest statistics {json.dumps(stats)} do not "
            f"account for all {expected} manifest documents (scanned={scanned}, "
            f"indexed={indexed}, failed={failed}). Refusing to stamp ingested: "
            "a silently skipped document would exist in arm P but not in this "
            "index, and the gap would read as a retrieval failure rather than "
            "an ingest failure."
        )
        return 1

    state = load_state(STATE_PATH)
    state[arm_id] = {"ingested": True, "stamp": make_ingest_stamp(MANIFEST_PATH)}
    save_state(state, STATE_PATH)
    return 0


def cmd_status(arm_id: str) -> int:
    cfg = load_config()
    acfg = _arm_config(cfg, arm_id)
    name = stack_name(arm_id)

    cfn = _session(cfg).client("cloudformation")
    out = stack_outputs(cfn, name)
    if not out:
        print(f"{arm_id}: stack {name} not deployed")
        return 0

    state = load_state(STATE_PATH).get(arm_id, {})
    report = {
        "arm": arm_id,
        "stack": name,
        "status": out.get("status"),
        "config_sha256_tag": out.get("tags", {}).get(TAG_KEY),
        "config_sha256_current": sha256_json(acfg),
        "knowledge_base_id": out.get("KnowledgeBaseId"),
        "data_source_id": out.get("DataSourceId"),
        "source_bucket": out.get("SourceBucketName"),
        "ingested": state.get("ingested", False),
    }
    if state.get("stamp"):
        report["manifest_drift"] = ingest_stamp_matches(state["stamp"], MANIFEST_PATH)
    print(json.dumps(report, indent=2))
    return 0


def cmd_destroy(arm_id: str) -> int:
    cfg = load_config()
    _arm_config(cfg, arm_id)
    name = stack_name(arm_id)

    sess = _session(cfg)
    cfn, s3 = sess.client("cloudformation"), sess.client("s3")
    out = stack_outputs(cfn, name)
    if not out:
        print(f"{arm_id}: stack {name} not deployed, nothing to destroy")
        return 0

    bucket = out.get("SourceBucketName")
    if bucket:
        n = _empty_bucket(s3, bucket)
        print(f"{arm_id}: emptied {n} object(s) from {bucket}")

    _cdk(["destroy", name, "--force"])
    _wait_delete_complete(cfn, name)
    print(f"{arm_id}: destroyed {name} (DELETE_COMPLETE confirmed)")

    state = load_state(STATE_PATH)
    if arm_id in state:
        del state[arm_id]
        save_state(state, STATE_PATH)
    return 0


COMMANDS = {
    "deploy": cmd_deploy,
    "upload": cmd_upload,
    "ingest": cmd_ingest,
    "status": cmd_status,
    "destroy": cmd_destroy,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--arm", required=True, help="arm id, e.g. B0-default-v1")
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args.arm)


if __name__ == "__main__":
    raise SystemExit(main())
