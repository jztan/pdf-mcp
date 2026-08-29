"""Stubber tests for the imperative Bedrock helpers. No AWS calls."""

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import ClientError  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

from scripts._bedrock_kb import (  # noqa: E402
    ingest,
    ingest_stamp_matches,
    load_state,
    make_ingest_stamp,
    save_state,
    sha256_json,
    stack_name,
    stack_outputs,
    upload,
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

    def test_other_client_error_propagates(self):
        # Only a ValidationError whose message says "does not exist" means
        # not deployed. Any other error (throttling, access denied, a
        # ValidationError about something else) must not be swallowed,
        # or a caller would go on to try to create a stack that already
        # exists.
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_client_error(
                "describe_stacks",
                service_error_code="Throttling",
                service_message="Rate exceeded",
            )
            with pytest.raises(ClientError):
                stack_outputs(cfn, "pdfmcp-anchor-b0-default-v1")

    def test_validation_error_with_different_message_propagates(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_client_error(
                "describe_stacks",
                service_error_code="ValidationError",
                service_message="1 validation error detected: bad StackName",
            )
            with pytest.raises(ClientError):
                stack_outputs(cfn, "pdfmcp-anchor-b0-default-v1")


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

    def test_wait_ingest_times_out_on_stuck_status(self):
        agent = _client("bedrock-agent")
        now = "2026-08-29T00:00:00Z"
        job = {
            "ingestionJobId": "J1",
            "knowledgeBaseId": "KB1",
            "dataSourceId": "DS1",
            "startedAt": now,
            "updatedAt": now,
            "status": "IN_PROGRESS",
        }
        with Stubber(agent) as st:
            st.add_response(
                "get_ingestion_job",
                {"ingestionJob": job},
                {
                    "knowledgeBaseId": "KB1",
                    "dataSourceId": "DS1",
                    "ingestionJobId": "J1",
                },
            )
            with pytest.raises(TimeoutError, match="IN_PROGRESS"):
                wait_ingest(agent, "KB1", "DS1", "J1", poll_s=0, max_wait_s=0)


class TestUpload:
    def test_uploads_manifest_relative_keys_and_verifies(self, tmp_path):
        # Two files sharing a basename in different directories must not
        # collide on the same S3 key.
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        f1 = tmp_path / "a" / "doc.pdf"
        f2 = tmp_path / "b" / "doc.pdf"
        f1.write_bytes(b"one")
        f2.write_bytes(b"two")

        s3 = _client("s3")
        with Stubber(s3) as st:
            st.add_response(
                "put_object",
                {},
                {"Bucket": "bkt", "Key": "a/doc.pdf", "Body": ANY},
            )
            st.add_response(
                "put_object",
                {},
                {"Bucket": "bkt", "Key": "b/doc.pdf", "Body": ANY},
            )
            st.add_response(
                "list_objects_v2",
                {
                    "Contents": [
                        {"Key": "a/doc.pdf"},
                        {"Key": "b/doc.pdf"},
                    ],
                    "IsTruncated": False,
                },
                {"Bucket": "bkt"},
            )
            count = upload(s3, "bkt", [f1, f2], tmp_path)
        assert count == 2

    def test_raises_when_confirmed_count_is_short(self, tmp_path):
        f1 = tmp_path / "doc.pdf"
        f1.write_bytes(b"one")

        s3 = _client("s3")
        with Stubber(s3) as st:
            st.add_response(
                "put_object",
                {},
                {"Bucket": "bkt", "Key": "doc.pdf", "Body": ANY},
            )
            st.add_response(
                "list_objects_v2",
                {"Contents": [], "IsTruncated": False},
                {"Bucket": "bkt"},
            )
            with pytest.raises(RuntimeError, match="upload verification failed"):
                upload(s3, "bkt", [f1], tmp_path)


class TestDuplicatedConstants:
    def test_stack_and_helpers_agree(self):
        pytest.importorskip("aws_cdk")
        import infra.bedrock_kb.app as cdk_app
        import infra.bedrock_kb.stack as cdk_stack

        import scripts._bedrock_kb as helpers

        assert cdk_stack.TAG_KEY == helpers.TAG_KEY
        assert cdk_stack.EMBED_MODEL == helpers.EMBED_MODEL
        assert cdk_stack.stack_name("X-y-v1") == helpers.stack_name("X-y-v1")

        payload = {"b": 2, "a": 1}
        assert cdk_app.sha256_json(payload) == helpers.sha256_json(payload)
