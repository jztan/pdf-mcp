"""CDK app: one stack per Bedrock arm in benchmark_data/bedrock_kb/config.json."""

import hashlib
import json
import sys
from pathlib import Path

import aws_cdk as cdk

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from infra.bedrock_kb.stack import BedrockArmStack  # noqa: E402

CONFIG = REPO / "benchmark_data" / "bedrock_kb" / "config.json"


def sha256_json(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def main() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    app = cdk.App()
    for arm_id, arm_cfg in cfg["arms"].items():
        if arm_id == "P":
            continue
        BedrockArmStack(
            app,
            arm_id,
            arm_cfg=arm_cfg,
            region=cfg["region"],
            config_sha256=sha256_json(arm_cfg),
        )
    app.synth()


if __name__ == "__main__":
    main()
