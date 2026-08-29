"""Synth-time tests for the Bedrock KB arm stack. No AWS calls."""

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
        t = _synth("B0-default-v1", {"strategy": "NONE_OVERRIDE_DEFAULT"})
        t.has_resource_properties(
            "AWS::S3::Bucket",
            {"BucketName": Match.object_like({"Fn::Join": Match.any_value()})},
        )
        t.has_resource_properties(
            "AWS::S3Vectors::VectorBucket",
            {"VectorBucketName": Match.object_like({"Fn::Join": Match.any_value()})},
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
