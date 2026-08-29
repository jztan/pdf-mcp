"""Tests for the bedrock_kb_stack CLI: deploy-refusal, arm-P rejection,
bucket emptying, and destroy-completion polling. No AWS calls; cdk and
boto3 clients are stubbed or monkeypatched.
"""

import json

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber  # noqa: E402

import scripts.bedrock_kb_stack as cli  # noqa: E402
from scripts._bedrock_kb import TAG_KEY  # noqa: E402


def _client(service):
    return boto3.Session(
        region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t"
    ).client(service)


class _FakeSession:
    """Stands in for boto3.Session in tests that stub stack_outputs/upload
    themselves and never let a real client make a network call."""

    def client(self, service_name):
        return object()


CFG = {
    "region": "us-east-1",
    "arms": {
        "P": {"tool": "pdf_corpus_search"},
        "B0-default-v1": {"label": "B0", "chunking": {"strategy": "X"}},
    },
}


class TestArmGuard:
    def test_arm_p_rejected_by_every_command(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        for name, fn in cli.COMMANDS.items():
            with pytest.raises(SystemExit, match="arm P has no AWS stack"):
                fn("P")

    def test_unknown_arm_rejected(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        with pytest.raises(SystemExit, match="unknown arm id"):
            cli.cmd_deploy("Z-nope-v1")


class TestDeployRefusal:
    def test_refuses_on_config_tag_mismatch(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        monkeypatch.setattr(cli, "_session", lambda cfg: _FakeSession())
        monkeypatch.setattr(
            cli,
            "stack_outputs",
            lambda cfn, name: {"tags": {TAG_KEY: "stale-hash"}},
        )

        def _boom(args):
            raise AssertionError("cdk must not run when the tag mismatches")

        monkeypatch.setattr(cli, "_cdk", _boom)

        rc = cli.cmd_deploy("B0-default-v1")
        assert rc == 2

    def test_proceeds_when_tag_matches(self, monkeypatch):
        acfg = CFG["arms"]["B0-default-v1"]
        current = cli.sha256_json(acfg)
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        monkeypatch.setattr(cli, "_session", lambda cfg: _FakeSession())
        monkeypatch.setattr(
            cli, "stack_outputs", lambda cfn, name: {"tags": {TAG_KEY: current}}
        )
        calls = []
        monkeypatch.setattr(cli, "_cdk", lambda args: calls.append(args))

        rc = cli.cmd_deploy("B0-default-v1")
        assert rc == 0
        assert calls == [
            ["deploy", "pdfmcp-anchor-b0-default-v1", "--require-approval", "never"]
        ]

    def test_proceeds_when_no_stack_exists_yet(self, monkeypatch):
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        monkeypatch.setattr(cli, "_session", lambda cfg: _FakeSession())
        monkeypatch.setattr(cli, "stack_outputs", lambda cfn, name: {})
        calls = []
        monkeypatch.setattr(cli, "_cdk", lambda args: calls.append(args))

        rc = cli.cmd_deploy("B0-default-v1")
        assert rc == 0
        assert len(calls) == 1


class TestCdkInvocation:
    def test_pins_the_running_interpreter_and_silences_jsii_warning(self, monkeypatch):
        captured = {}

        def fake_run(cmd, cwd, env, check):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["env"] = env
            captured["check"] = check

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        cli._cdk(["deploy", "some-stack"])

        assert captured["cmd"][:2] == ["npx", "aws-cdk@2"]
        assert "some-stack" in captured["cmd"]
        assert captured["cmd"][-2:] == ["--app", f"{cli.sys.executable} app.py"]
        assert captured["cwd"] == cli.INFRA_DIR
        assert captured["env"]["JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION"] == "1"
        assert captured["check"] is True


class TestCmdIngest:
    """cmd_ingest must refuse to stamp `ingested` unless the ingestion job's
    own statistics account for every manifest document: this is the
    invariant the README calls load-bearing (100 scanned, 100 indexed, 0
    failed), because a silently skipped document would exist in arm P but
    not in the Bedrock index, and the gap would read as a retrieval
    failure rather than an ingest failure."""

    def _setup(self, monkeypatch, tmp_path, stats):
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {"docs": [{"id": "a", "path": "a.pdf"}, {"id": "b", "path": "b.pdf"}]}
            )
        )
        state_path = tmp_path / ".stack.json"

        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        monkeypatch.setattr(cli, "_session", lambda cfg: _FakeSession())
        monkeypatch.setattr(
            cli,
            "stack_outputs",
            lambda cfn, name: {"KnowledgeBaseId": "kb", "DataSourceId": "ds"},
        )
        monkeypatch.setattr(cli, "ingest", lambda agent, kb, ds: "job1")
        monkeypatch.setattr(
            cli,
            "wait_ingest",
            lambda agent, kb, ds, job_id, poll_s=20: {
                "status": "COMPLETE",
                "statistics": stats,
            },
        )
        monkeypatch.setattr(cli, "MANIFEST_PATH", manifest_path)
        monkeypatch.setattr(cli, "STATE_PATH", state_path)
        return state_path

    def test_stamps_ingested_when_statistics_account_for_every_document(
        self, monkeypatch, tmp_path
    ):
        state_path = self._setup(
            monkeypatch,
            tmp_path,
            {
                "numberOfDocumentsScanned": 2,
                "numberOfNewDocumentsIndexed": 2,
                "numberOfModifiedDocumentsIndexed": 0,
                "numberOfDocumentsFailed": 0,
            },
        )
        rc = cli.cmd_ingest("B0-default-v1")
        assert rc == 0
        assert cli.load_state(state_path)["B0-default-v1"]["ingested"] is True

    def test_refuses_when_scanned_is_short_of_manifest_count(
        self, monkeypatch, tmp_path
    ):
        state_path = self._setup(
            monkeypatch,
            tmp_path,
            {
                "numberOfDocumentsScanned": 1,
                "numberOfNewDocumentsIndexed": 1,
                "numberOfModifiedDocumentsIndexed": 0,
                "numberOfDocumentsFailed": 0,
            },
        )
        rc = cli.cmd_ingest("B0-default-v1")
        assert rc == 1
        assert cli.load_state(state_path) == {}

    def test_refuses_when_a_document_failed_even_if_counts_otherwise_match(
        self, monkeypatch, tmp_path
    ):
        state_path = self._setup(
            monkeypatch,
            tmp_path,
            {
                "numberOfDocumentsScanned": 2,
                "numberOfNewDocumentsIndexed": 2,
                "numberOfModifiedDocumentsIndexed": 0,
                "numberOfDocumentsFailed": 1,
            },
        )
        rc = cli.cmd_ingest("B0-default-v1")
        assert rc == 1
        assert cli.load_state(state_path) == {}


class TestEmptyBucket:
    def test_paginates_and_deletes_every_object(self):
        s3 = _client("s3")
        with Stubber(s3) as st:
            st.add_response(
                "list_objects_v2",
                {
                    "Contents": [{"Key": "a.pdf"}, {"Key": "b.pdf"}],
                    "IsTruncated": False,
                },
                {"Bucket": "bkt"},
            )
            st.add_response(
                "delete_objects",
                {},
                {
                    "Bucket": "bkt",
                    "Delete": {"Objects": [{"Key": "a.pdf"}, {"Key": "b.pdf"}]},
                },
            )
            n = cli._empty_bucket(s3, "bkt")
        assert n == 2

    def test_empty_bucket_is_a_noop_on_an_empty_bucket(self):
        s3 = _client("s3")
        with Stubber(s3) as st:
            st.add_response(
                "list_objects_v2",
                {"Contents": [], "IsTruncated": False},
                {"Bucket": "bkt"},
            )
            n = cli._empty_bucket(s3, "bkt")
        assert n == 0


class TestWaitDeleteComplete:
    def test_returns_once_the_stack_is_gone(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "s",
                            "StackStatus": "DELETE_IN_PROGRESS",
                            "CreationTime": "2026-08-29T00:00:00Z",
                        }
                    ]
                },
                {"StackName": "s"},
            )
            st.add_client_error(
                "describe_stacks",
                service_error_code="ValidationError",
                service_message="Stack with id s does not exist",
            )
            cli._wait_delete_complete(cfn, "s", poll_s=0, max_wait_s=5)

    def test_raises_on_failed_status(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "s",
                            "StackStatus": "DELETE_FAILED",
                            "CreationTime": "2026-08-29T00:00:00Z",
                        }
                    ]
                },
                {"StackName": "s"},
            )
            with pytest.raises(RuntimeError, match="DELETE_FAILED"):
                cli._wait_delete_complete(cfn, "s", poll_s=0, max_wait_s=5)

    def test_times_out_on_stuck_delete_in_progress(self):
        cfn = _client("cloudformation")
        with Stubber(cfn) as st:
            st.add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "s",
                            "StackStatus": "DELETE_IN_PROGRESS",
                            "CreationTime": "2026-08-29T00:00:00Z",
                        }
                    ]
                },
                {"StackName": "s"},
            )
            with pytest.raises(TimeoutError, match="DELETE_IN_PROGRESS"):
                cli._wait_delete_complete(cfn, "s", poll_s=0, max_wait_s=0)


class TestDestroyNeverActuallyRuns:
    """cmd_destroy must be exercised only through stubs/mocks in this repo.

    This test proves the wiring (empty bucket -> cdk destroy -> confirm
    DELETE_COMPLETE -> drop .stack.json entry) without ever shelling out to
    the real CDK CLI or touching a live account.
    """

    def test_full_sequence_with_mocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "load_config", lambda: CFG)
        monkeypatch.setattr(cli, "STATE_PATH", tmp_path / ".stack.json")
        cli.save_state({"B0-default-v1": {"ingested": True}}, cli.STATE_PATH)

        monkeypatch.setattr(cli, "_session", lambda cfg: _FakeSession())
        monkeypatch.setattr(
            cli,
            "stack_outputs",
            lambda cfn, name: {"SourceBucketName": "bkt", "status": "CREATE_COMPLETE"},
        )
        emptied = []
        monkeypatch.setattr(
            cli, "_empty_bucket", lambda s3, bucket: emptied.append(bucket) or 100
        )
        destroyed = []
        monkeypatch.setattr(cli, "_cdk", lambda args: destroyed.append(args))
        monkeypatch.setattr(cli, "_wait_delete_complete", lambda cfn, name: None)

        rc = cli.cmd_destroy("B0-default-v1")
        assert rc == 0
        assert emptied == ["bkt"]
        assert destroyed == [["destroy", "pdfmcp-anchor-b0-default-v1", "--force"]]
        assert cli.load_state(cli.STATE_PATH) == {}
