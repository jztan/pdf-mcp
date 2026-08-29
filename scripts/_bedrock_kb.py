"""Imperative helpers the CDK stack cannot express.

Provisioning and teardown live in infra/bedrock_kb (CDK). This file only
does: read stack outputs, upload PDFs, start and wait for ingestion,
retrieve, rerank, and the stamps that make an index's provenance checkable.
Keep benchmark logic out of this file.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from pathlib import Path

from botocore.exceptions import ClientError

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
RERANK_MODEL = "cohere.rerank-v3-5:0"
TAG_KEY = "pdfmcp:arm_config_sha256"


def stack_name(arm_id: str) -> str:
    return f"pdfmcp-anchor-{arm_id.lower()}"


def sha256_json(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def make_ingest_stamp(manifest_path: Path) -> dict:
    return {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "ingested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def ingest_stamp_matches(stamp: dict, manifest_path: Path) -> list[str]:
    cur = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return [] if stamp.get("manifest_sha256") == cur else ["manifest"]


def stack_outputs(cfn, name: str) -> dict:
    """Outputs + tags + status of a deployed stack; {} if it does not exist.

    Only a ValidationError whose message says the stack does not exist is
    treated as "not deployed". Any other ClientError (throttling, access
    denied, an unrelated validation failure) is re-raised, since silently
    reading those as "not deployed" would make a caller try to create a
    stack that is already there.
    """
    try:
        stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as e:
        error = e.response.get("Error", {})
        code = error.get("Code")
        message = error.get("Message", "")
        if code == "ValidationError" and "does not exist" in message:
            return {}
        raise
    out = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    out["tags"] = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
    out["status"] = stack["StackStatus"]
    return out


def upload(s3, bucket: str, files: list[Path], base_dir: Path) -> int:
    """Upload files to bucket, keyed by their path relative to base_dir.

    Keying on the relative path (not just the basename) keeps two manifest
    documents with the same filename in different directories from
    overwriting each other in the bucket. The upload is then verified
    against a listing of the bucket rather than trusting the request: if
    the confirmed count does not match what was requested, raise instead
    of reporting a corpus that is quietly short.
    """
    base = Path(base_dir).resolve()
    keys = []
    for f in files:
        key = f.resolve().relative_to(base).as_posix()
        s3.put_object(Bucket=bucket, Key=key, Body=f.read_bytes())
        keys.append(key)

    present: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        present.update(o["Key"] for o in page.get("Contents", []))

    confirmed = sum(1 for k in keys if k in present)
    if confirmed != len(files):
        raise RuntimeError(
            f"upload verification failed: requested {len(files)} files, "
            f"confirmed {confirmed} present in s3://{bucket}"
        )
    return confirmed


def ingest(agent, kb_id: str, ds_id: str) -> str:
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    return job["ingestionJob"]["ingestionJobId"]


def wait_ingest(
    agent,
    kb_id: str,
    ds_id: str,
    job_id: str,
    poll_s: float = 15,
    max_wait_s: float = 3600,
) -> dict:
    """Poll an ingestion job until it reaches a terminal status.

    Terminal today: COMPLETE, FAILED, STOPPED (STOPPING correctly keeps
    polling). Any status this does not recognize as terminal, including
    one AWS adds later or a throttling anomaly, is bounded by max_wait_s
    rather than polling forever with no output.
    """
    start = time.monotonic()
    while True:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = job["status"]
        if status in {"COMPLETE", "FAILED", "STOPPED"}:
            return job
        elapsed = time.monotonic() - start
        if elapsed >= max_wait_s:
            raise TimeoutError(
                f"ingestion job {job_id} still {status!r} after "
                f"{elapsed:.1f}s (max_wait_s={max_wait_s})"
            )
        time.sleep(poll_s)
