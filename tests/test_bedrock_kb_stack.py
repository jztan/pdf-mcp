"""Stubber tests for the imperative Bedrock helpers. No AWS calls."""

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber  # noqa: E402

from scripts._bedrock_kb import (  # noqa: E402
    ingest,
    ingest_stamp_matches,
    load_state,
    make_ingest_stamp,
    save_state,
    sha256_json,
    stack_name,
    stack_outputs,
    wait_ingest,
)


def _client(service):
    return boto3.Session(
        region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t"
    ).client(service)


class TestState:
    def test_round_trip(self, tmp_path):
        p = tmp_path / ".stack.json"
        save_state({"B0-default-v1": {"ingested": True}}, p)
        assert load_state(p) == {"B0-default-v1": {"ingested": True}}

    def test_missing_state_is_empty(self, tmp_path):
        assert load_state(tmp_path / "nope.json") == {}


class TestStamps:
    def test_sha256_json_is_order_independent(self):
        assert sha256_json({"a": 1, "b": 2}) == sha256_json({"b": 2, "a": 1})

    def test_ingest_stamp_detects_manifest_change(self, tmp_path):
        m = tmp_path / "manifest.json"
        m.write_text('{"docs": []}')
        st = make_ingest_stamp(m)
        assert set(st) == {"manifest_sha256", "ingested_at"}
        assert ingest_stamp_matches(st, m) == []
        m.write_text('{"docs": [1]}')
        assert ingest_stamp_matches(st, m) == ["manifest"]


class TestStackOutputs:
    def test_name(self):
        assert stack_name("B1-fixed1000-v1") == "pdfmcp-anchor-b1-fixed1000-v1"

    def test_reads_outputs_and_tags(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "pdfmcp-anchor-b0-default-v1",
                            "StackStatus": "CREATE_COMPLETE",
                            "CreationTime": "2026-08-29T00:00:00Z",
                            "Outputs": [
                                {"OutputKey": "KnowledgeBaseId", "OutputValue": "KB1"},
                                {"OutputKey": "DataSourceId", "OutputValue": "DS1"},
                            ],
                            "Tags": [{"Key": "pdfmcp:arm_config_sha256", "Value": "h"}],
                        }
                    ]
                },
                {"StackName": "pdfmcp-anchor-b0-default-v1"},
            )
            out = stack_outputs(cfn, "pdfmcp-anchor-b0-default-v1")
        assert out == {
            "KnowledgeBaseId": "KB1",
            "DataSourceId": "DS1",
            "tags": {"pdfmcp:arm_config_sha256": "h"},
            "status": "CREATE_COMPLETE",
        }

    def test_missing_stack_is_empty(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_client_error(
                "describe_stacks",
                service_error_code="ValidationError",
                service_message="Stack with id x does not exist",
            )
            assert stack_outputs(cfn, "x") == {}


class TestIngest:
    def test_ingest_then_wait_until_complete(self):
        agent = _client("bedrock-agent")
        now = "2026-08-29T00:00:00Z"
        job = {
            "ingestionJobId": "J1",
            "knowledgeBaseId": "KB1",
            "dataSourceId": "DS1",
            "startedAt": now,
            "updatedAt": now,
        }
        with Stubber(agent) as st:
            st.add_response(
                "start_ingestion_job",
                {"ingestionJob": {**job, "status": "STARTING"}},
                {"knowledgeBaseId": "KB1", "dataSourceId": "DS1"},
            )
            st.add_response(
                "get_ingestion_job",
                {"ingestionJob": {**job, "status": "IN_PROGRESS"}},
                {
                    "knowledgeBaseId": "KB1",
                    "dataSourceId": "DS1",
                    "ingestionJobId": "J1",
                },
            )
            st.add_response(
                "get_ingestion_job",
                {"ingestionJob": {**job, "status": "COMPLETE"}},
                {
                    "knowledgeBaseId": "KB1",
                    "dataSourceId": "DS1",
                    "ingestionJobId": "J1",
                },
            )
            jid = ingest(agent, "KB1", "DS1")
            done = wait_ingest(agent, "KB1", "DS1", jid, poll_s=0)
        assert jid == "J1" and done["status"] == "COMPLETE"
