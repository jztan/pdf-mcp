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
    """Outputs + tags + status of a deployed stack; {} if it does not exist."""
    try:
        stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as e:
        if "does not exist" in str(e):
            return {}
        raise
    out = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    out["tags"] = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
    out["status"] = stack["StackStatus"]
    return out


def upload(s3, bucket: str, files: list[Path]) -> int:
    for f in files:
        s3.upload_file(str(f), bucket, f.name)
    return len(files)


def ingest(agent, kb_id: str, ds_id: str) -> str:
    job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
    return job["ingestionJob"]["ingestionJobId"]


def wait_ingest(agent, kb_id: str, ds_id: str, job_id: str, poll_s: float = 15) -> dict:
    while True:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        if job["status"] in {"COMPLETE", "FAILED", "STOPPED"}:
            return job
        time.sleep(poll_s)
