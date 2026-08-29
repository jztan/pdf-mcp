"""Synth-time tests for the Bedrock KB arm stack. No AWS calls."""

import json

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template  # noqa: E402

from infra.bedrock_kb.stack import BedrockArmStack  # noqa: E402


def _synth(arm_id, chunking):
    app = cdk.App()
    stack = BedrockArmStack(
        app,
        arm_id,
        arm_cfg={"parser": "default", "chunking": chunking, "rerank": None},
        region="us-east-1",
        config_sha256="abc123",
    )
    return Template.from_stack(stack)


class TestBedrockArmStack:
    def test_creates_exactly_the_six_resources(self):
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.resource_count_is("AWS::S3::Bucket", 1)
        t.resource_count_is("AWS::S3Vectors::VectorBucket", 1)
        t.resource_count_is("AWS::S3Vectors::Index", 1)
        t.resource_count_is("AWS::IAM::Role", 1)
        t.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)
        t.resource_count_is("AWS::Bedrock::DataSource", 1)

    def test_kb_uses_s3_vectors_and_titan_v2(self):
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::Bedrock::KnowledgeBase",
            {
                "StorageConfiguration": {"Type": "S3_VECTORS"},
                "KnowledgeBaseConfiguration": {
                    "Type": "VECTOR",
                    "VectorKnowledgeBaseConfiguration": {
                        "EmbeddingModelArn": Match.string_like_regexp(
                            "titan-embed-text-v2:0$"
                        )
                    },
                },
            },
        )

    def test_index_is_1024_cosine_float32(self):
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::S3Vectors::Index",
            {"DataType": "float32", "Dimension": 1024, "DistanceMetric": "cosine"},
        )

    def test_index_vector_bucket_name_is_the_name_not_the_arn(self):
        """CfnVectorBucket.ref is the ARN (92 chars); VectorBucketName caps at
        63, so passing .ref fails CloudFormation validation at deploy time.
        Synth alone cannot catch it, so assert the rendered value is a Join of
        the literal name segments and never contains an arn prefix."""
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        index = list(t.find_resources("AWS::S3Vectors::Index").values())[0]
        rendered = json.dumps(index["Properties"]["VectorBucketName"])
        assert "arn:" not in rendered
        assert "pdfmcp-anchor-b0-default-v1-vec-" in rendered

    def test_index_non_filterable_keys_include_both_managed_keys(self):
        # Non-filterable designation is immutable after index creation.
        # Missing AMAZON_BEDROCK_METADATA means Bedrock's own managed
        # metadata counts against the filterable budget and 35-key limit,
        # and ingestion throws once that is exceeded.
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::S3Vectors::Index",
            {
                "MetadataConfiguration": {
                    "NonFilterableMetadataKeys": Match.array_with(
                        ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]
                    )
                }
            },
        )

    def test_default_chunking_sets_no_chunking_configuration(self):
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::Bedrock::DataSource",
            {"VectorIngestionConfiguration": Match.absent()},
        )

    def test_fixed_chunking_is_passed_through(self):
        t = _synth(
            "B1-fixed1000-v1",
            {"strategy": "FIXED_SIZE", "maxTokens": 1000, "overlapPercentage": 20},
        )
        t.has_resource_properties(
            "AWS::Bedrock::DataSource",
            {
                "VectorIngestionConfiguration": {
                    "ChunkingConfiguration": {
                        "ChunkingStrategy": "FIXED_SIZE",
                        "FixedSizeChunkingConfiguration": {
                            "MaxTokens": 1000,
                            "OverlapPercentage": 20,
                        },
                    }
                }
            },
        )

    def test_config_hash_is_a_stack_tag_and_outputs_exist(self):
        app = cdk.App()
        stack = BedrockArmStack(
            app,
            "B0-default-v1",
            arm_cfg={"chunking": {"strategy": "NONE_OVERRIDE_DEFAULT"}},
            region="us-east-1",
            config_sha256="abc123",
        )
        assert stack.stack_name == "pdfmcp-anchor-b0-default-v1"
        t = Template.from_stack(stack)
        # tags propagate to taggable resources; check the bucket carries it
        t.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "Tags": Match.array_with(
                    [{"Key": "pdfmcp:arm_config_sha256", "Value": "abc123"}]
                )
            },
        )
        for key in (
            "KnowledgeBaseId",
            "DataSourceId",
            "SourceBucketName",
            "VectorIndexArn",
        ):
            t.has_output(key, {})

    def test_explicit_names_carry_the_stack_prefix(self):
        # self.account is an unresolved CDK token at synth time, so these
        # render as Fn::Join rather than a literal string. Keep the join
        # shape but still assert the literal prefix segment inside it.
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "BucketName": Match.object_like(
                    {
                        "Fn::Join": Match.array_with(
                            [
                                "",
                                Match.array_with(["pdfmcp-anchor-b0-default-v1-src-"]),
                            ]
                        )
                    }
                )
            },
        )
        t.has_resource_properties(
            "AWS::S3Vectors::VectorBucket",
            {
                "VectorBucketName": Match.object_like(
                    {
                        "Fn::Join": Match.array_with(
                            [
                                "",
                                Match.array_with(["pdfmcp-anchor-b0-default-v1-vec-"]),
                            ]
                        )
                    }
                )
            },
        )
        t.has_resource_properties(
            "AWS::IAM::Role", {"RoleName": "pdfmcp-anchor-b0-default-v1-kb-role"}
        )
        t.has_resource_properties(
            "AWS::Bedrock::KnowledgeBase", {"Name": "pdfmcp-anchor-b0-default-v1"}
        )
        t.has_resource_properties(
            "AWS::Bedrock::DataSource", {"Name": "pdfmcp-anchor-b0-default-v1-src"}
        )

    def test_role_is_scoped_to_this_arm_only(self):
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {"Principal": {"Service": "bedrock.amazonaws.com"}}
                            )
                        ]
                    )
                }
            },
        )
        t.resource_count_is("AWS::IAM::Policy", 1)

        policies = t.find_resources("AWS::IAM::Policy")
        assert len(policies) == 1
        statements = list(policies.values())[0]["Properties"]["PolicyDocument"][
            "Statement"
        ]

        def _resources(stmt):
            r = stmt["Resource"]
            return r if isinstance(r, list) else [r]

        # No statement is left wildcard-scoped.
        for stmt in statements:
            for resource in _resources(stmt):
                assert resource != "*", stmt

        embed_arn = (
            "arn:aws:bedrock:us-east-1::foundation-model/"
            "amazon.titan-embed-text-v2:0"
        )
        invoke_model = [s for s in statements if s["Action"] == "bedrock:InvokeModel"]
        assert len(invoke_model) == 1
        assert _resources(invoke_model[0]) == [embed_arn]

        s3vectors_statements = [
            s
            for s in statements
            if isinstance(s["Action"], list)
            and any(a.startswith("s3vectors:") for a in s["Action"])
        ]
        assert len(s3vectors_statements) == 1
        assert _resources(s3vectors_statements[0]) == [
            {"Fn::GetAtt": ["Index", "IndexArn"]}
        ]

    def test_kb_depends_on_role_policy_and_index(self):
        # Holds today only because CDK expands a construct-level
        # node.add_dependency(role) over the role's whole subtree,
        # including its inline DefaultPolicy. Switching to
        # attach_inline_policy or a managed policy would silently drop
        # this and produce an intermittent role-validation failure at KB
        # creation, so assert the expansion directly rather than trust it.
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        kbs = t.find_resources("AWS::Bedrock::KnowledgeBase")
        assert len(kbs) == 1
        depends_on = list(kbs.values())[0].get("DependsOn", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        assert any("DefaultPolicy" in d for d in depends_on), depends_on
        assert any(d.startswith("Index") for d in depends_on), depends_on
