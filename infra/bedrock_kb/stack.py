"""One CloudFormation stack per Bedrock arm.

Every resource is pay-per-use with zero idle cost: S3, S3 Vectors, Bedrock
Knowledge Bases. Nothing else may be added here. The stack is tagged with
the sha256 of its arm config; that tag is the declarative record of what
the index was built from, and the deploy wrapper refuses to deploy over a
stack whose tag differs (indexes are immutable; a config change is a new
arm id and a new stack).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from constructs import Construct

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
EMBED_DIM = 1024
TAG_KEY = "pdfmcp:arm_config_sha256"


def stack_name(arm_id: str) -> str:
    return f"pdfmcp-anchor-{arm_id.lower()}"


def _vector_kb_config(embed_arn: str):
    prop = bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty
    return prop(embedding_model_arn=embed_arn)


def _s3_data_source_config(bucket_arn: str):
    prop = bedrock.CfnDataSource.S3DataSourceConfigurationProperty
    return prop(bucket_arn=bucket_arn)


def _chunking_property(chunking: dict | None):
    if not chunking or chunking.get("strategy") == "NONE_OVERRIDE_DEFAULT":
        return None
    if chunking["strategy"] == "FIXED_SIZE":
        return bedrock.CfnDataSource.ChunkingConfigurationProperty(
            chunking_strategy="FIXED_SIZE",
            fixed_size_chunking_configuration=(
                bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                    max_tokens=chunking["maxTokens"],
                    overlap_percentage=chunking["overlapPercentage"],
                )
            ),
        )
    raise ValueError(f"unsupported chunking strategy {chunking['strategy']}")


class BedrockArmStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        arm_id: str,
        *,
        arm_cfg: dict,
        region: str,
        config_sha256: str,
        **kwargs,
    ) -> None:
        super().__init__(
            scope,
            stack_name(arm_id),
            stack_name=stack_name(arm_id),
            env=cdk.Environment(region=region),
            **kwargs,
        )
        cdk.Tags.of(self).add(TAG_KEY, config_sha256)
        cdk.Tags.of(self).add("pdfmcp:arm_id", arm_id)

        # Source PDFs. DESTROY so `cdk destroy` is clean when (rarely)
        # wanted; the spec's default is to keep the stack. Not
        # auto-delete-objects: that CDK feature injects a Lambda-backed
        # custom resource (its own IAM role, function, log group) purely
        # to empty the bucket first, which is imperative plumbing this
        # arm-per-stack design keeps out of CloudFormation.
        name = stack_name(arm_id)
        bucket = s3.Bucket(
            self,
            "Source",
            bucket_name=f"{name}-src-{self.account}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        vbucket = s3vectors.CfnVectorBucket(
            self, "VectorBucket", vector_bucket_name=f"{name}-vec-{self.account}"
        )
        index = s3vectors.CfnIndex(
            self,
            "Index",
            vector_bucket_name=vbucket.ref,
            index_name="chunks",
            data_type="float32",
            dimension=EMBED_DIM,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT"]
            ),
        )
        index.add_dependency(vbucket)

        embed_arn = f"arn:aws:bedrock:{region}::foundation-model/{EMBED_MODEL}"
        role = iam.Role(
            self,
            "KbRole",
            role_name=f"{name}-kb-role",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        bucket.grant_read(role)
        role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[embed_arn])
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:QueryVectors",
                    "s3vectors:DeleteVectors",
                ],
                resources=[index.attr_index_arn],
            )
        )

        kb = bedrock.CfnKnowledgeBase(
            self,
            "Kb",
            name=stack_name(arm_id),
            role_arn=role.role_arn,
            knowledge_base_configuration=(
                bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                    type="VECTOR",
                    vector_knowledge_base_configuration=_vector_kb_config(embed_arn),
                )
            ),
            storage_configuration=(
                bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                    type="S3_VECTORS",
                    s3_vectors_configuration=(
                        bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                            index_arn=index.attr_index_arn
                        )
                    ),
                )
            ),
        )
        # Bedrock validates the role at create time; make CFN wait for the
        # role's inline policy, not just the role, to dodge IAM propagation.
        kb.node.add_dependency(role)
        kb.node.add_dependency(index)

        ds_kwargs: dict = {}
        chunking = _chunking_property(arm_cfg.get("chunking"))
        if chunking is not None:
            ds_kwargs["vector_ingestion_configuration"] = (
                bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                    chunking_configuration=chunking
                )
            )
        ds = bedrock.CfnDataSource(
            self,
            "DataSource",
            name=f"{stack_name(arm_id)}-src",
            knowledge_base_id=kb.attr_knowledge_base_id,
            data_source_configuration=(
                bedrock.CfnDataSource.DataSourceConfigurationProperty(
                    type="S3",
                    s3_configuration=_s3_data_source_config(bucket.bucket_arn),
                )
            ),
            # RETAIN: deleting a data source with DELETE would also purge the
            # vectors; destroy order is handled by the stack anyway.
            data_deletion_policy="RETAIN",
            **ds_kwargs,
        )

        cdk.CfnOutput(self, "KnowledgeBaseId", value=kb.attr_knowledge_base_id)
        cdk.CfnOutput(self, "DataSourceId", value=ds.attr_data_source_id)
        cdk.CfnOutput(self, "SourceBucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "VectorIndexArn", value=index.attr_index_arn)
