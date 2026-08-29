# Bedrock KB anchor benchmark: runbook

Compares pdf-mcp corpus search (arm P) against Amazon Bedrock Knowledge Bases
(arms B0 and B1) on the existing 100-document `corpus_search` benchmark, scored by
evidence-span containment at an equal token budget, reported per query class.

**Bedrock is an anchor, not a subject.** Every pdf-mcp number so far is pdf-mcp
compared to itself; this gives those numbers an outside reference point. Any
result is acceptable, including "pdf-mcp equals the anchor". There is no kill
condition and no thesis being tested.

Design: `docs/superpowers/specs/2026-08-29-bedrock-kb-comparison-design.md`
Plan: `docs/superpowers/plans/2026-08-29-bedrock-kb-anchor.md`
Branch: `feat/bedrock-kb-anchor`, worktree `/Users/jztan/src/pdf-mcp-bedrock-kb`

## State as of 2026-08-29

| Piece | File | Status |
|---|---|---|
| Corpus warm | (cache) | DONE, 100/100 docs, text + embeddings |
| Scoring harness | `scripts/benchmark_bedrock_kb.py` | DONE, no CLI yet |
| Arm P adapter | `scripts/benchmark_bedrock_kb.py::run_arm_p` | DONE, runs live |
| CDK stacks | `infra/bedrock_kb/` | DONE, synth-tested, NOT deployed |
| boto3 helpers | `scripts/_bedrock_kb.py` | DONE, stub-tested |
| Arm configs | `benchmark_data/bedrock_kb/config.json` | DONE, committed |
| Stack CLI | `scripts/bedrock_kb_stack.py` | **NOT BUILT** (plan Task 8) |
| Bedrock arm adapter | `retrieve` / `rerank` / `run_arm_bedrock` | **NOT BUILT** (plan Task 9) |
| Benchmark CLI + run | `main()` in the harness | **NOT BUILT** (plan Task 10) |

Nothing has touched AWS. No resource exists, no spend has occurred.

## Prerequisites

```bash
cd /Users/jztan/src/pdf-mcp-bedrock-kb
uv sync --extra dev --extra bedrock
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1   # node 26 > jsii's tested range
aws sts get-caller-identity        # confirm the right account
aws configure get region           # must be us-east-1
```

Run the tests to confirm the tree is sound (55 tests, no AWS, no spend):

```bash
uv run pytest tests/test_benchmark_bedrock_kb.py tests/test_bedrock_kb_infra.py \
              tests/test_bedrock_kb_stack.py -q
```

## What remains, in order

### Task 8: deploy and ingest (first spend, about $0.10)

Build `scripts/bedrock_kb_stack.py` per plan Task 8, then:

```bash
# 1. Budget alarm FIRST, before anything billable
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
aws budgets create-budget --account-id "$ACCOUNT" \
  --budget '{"BudgetName":"pdfmcp-anchor","BudgetLimit":{"Amount":"5","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \
  --notifications-with-subscribers '[
    {"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":50},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"<your-email>"}]},
    {"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":100},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"<your-email>"}]}
  ]'

# 2. One-time CDK bootstrap
cd infra/bedrock_kb
npx aws-cdk@2 bootstrap "aws://$ACCOUNT/us-east-1"
npx aws-cdk@2 synth        # sanity: renders both templates locally, free
cd ../..

# 3. Per arm
uv run python scripts/bedrock_kb_stack.py deploy --arm B0-default-v1
uv run python scripts/bedrock_kb_stack.py upload --arm B0-default-v1
uv run python scripts/bedrock_kb_stack.py ingest --arm B0-default-v1
# then the same three for B1-fixed1000-v1
```

**Ingest must report 100 scanned, 100 indexed, 0 failed.** If any document
failed, stop and read `failureReasons`. A silently skipped document exists in
arm P but not in B0/B1, and the gap would read as a retrieval failure rather
than an ingest failure.

### Task 9: Bedrock arm adapter

Add `retrieve` and `rerank` to `scripts/_bedrock_kb.py`, and
`bedrock_results_to_units` plus `run_arm_bedrock` to the harness. See plan
Task 9 for the exact code and tests.

### Task 10: run it

Add `main()` to the harness, then the 20-query pilot at three budgets, then the
full run. Results land in `results.json` and `RESULTS.md` here.

## Cost

Measured inputs: 235 MB of PDFs, 2,238 pages, ~1.6M tokens, 89 queries.

| Item | Unit price | Cost |
|---|---|---|
| Titan Text Embeddings v2 | $0.02 / 1M tokens | $0.03 per ingest |
| S3 Vectors PUT | $0.20 / GB | $0.01 total |
| S3 Vectors storage | $0.06 / GB-month | under $0.01 / month |
| S3 source objects | $0.023 / GB-month | $0.01 / month |
| S3 Vectors queries | $2.50 / 1M | under $0.01 |
| Cohere Rerank 3.5 (B1 only) | $2.00 / 1,000 queries | $0.18 per run |

**Whole experiment: under $1.** Nothing bills for idle. Bedrock Data Automation
($0.010/page, about $22 for this corpus) is deliberately out of scope.

## Resources created

Per arm, one CloudFormation stack `pdfmcp-anchor-<arm id lowercased>` holding six
resources:

| Resource | Name |
|---|---|
| `AWS::S3::Bucket` | `<stack>-src-<account>` |
| `AWS::S3Vectors::VectorBucket` | `<stack>-vec-<account>` |
| `AWS::S3Vectors::Index` | `chunks` |
| `AWS::IAM::Role` + `Policy` | `<stack>-kb-role` |
| `AWS::Bedrock::KnowledgeBase` | `<stack>` |
| `AWS::Bedrock::DataSource` | `<stack>-src` |

Tagged `pdfmcp:arm_id` and `pdfmcp:arm_config_sha256` on every resource, so a
console search on `pdfmcp-anchor` or a tag filter finds all of it. Plus a
one-time `CDKToolkit` stack from bootstrap.

## Keep the indexes, do not tear them down

Storage is about $0.01 a month for both indexes. Deleting them saves nothing and
destroys an ingest you already paid for. Knowledge bases are free to exist and
are required for the `Retrieve` API, so deleting one breaks reuse.

Teardown exists for reproducibility and for rolling back a botched ingest, not as
an end-of-run step:

```bash
uv run python scripts/bedrock_kb_stack.py destroy --arm B0-default-v1
```

It must empty the source bucket via boto3 before `cdk destroy`:
`auto_delete_objects` is deliberately not set (it would add a Lambda), so
`RemovalPolicy.DESTROY` alone cannot delete a bucket holding the 100 PDFs.

## Revision rule

**An arm id names one immutable index.** To change a config, add a new id with a
bumped `-vN` suffix to `config.json` and deploy that; never edit an existing id in
place. The stack is tagged with the sha256 of its arm config, `deploy` refuses to
change an existing stack's config, `ingest` stamps the corpus manifest hash, and
the benchmark refuses to score an arm whose tag or stamp has drifted.

## Traps already hit, do not re-learn these

- **The vector index needs BOTH `AMAZON_BEDROCK_METADATA` and
  `AMAZON_BEDROCK_TEXT`** as non-filterable metadata keys. This is immutable
  after index creation; getting it wrong means destroying the index and
  re-ingesting. Already fixed in `stack.py` and guarded by a test.
- **Bedrock KB caps documents at 50 MB.** All 100 pass (largest 31.4 MB), but the
  400-doc distractor pool's largest is 47.8 MB, so a scale follow-up sits 2 MB
  from the cap. An over-quota file is silently skipped at ingest.
- **Hierarchical chunking is unavailable** with an S3 vector bucket (metadata size
  limits). **Semantic chunking** rejects files over 1 MB of body text.
  **No-chunking** fails on anything over Titan's 8,192-token limit. That is why
  only default and fixed-1000 are used.
- **Never lift arm P's numbers from `modes_results.md`.** Runs from different
  cache warms are not comparable: that file records the semantic arm moving 0.107
  on needle page-NDCG with zero code change. Arm P must be re-run in-session.
- **Arm P returns up to top_k=25 units** inside the 2,000-token budget, because
  pdf-mcp's paragraph excerpts are short. Bedrock's ~300-token chunks will
  exhaust the same budget in about 6. That is the budget rule working, not a bug.
  The plan's "P should be 3 to 5" sanity check is wrong.
- **Background Python must be a file, never piped on stdin.** `multiprocessing`
  cannot re-import a stdin script, so the warm pool crashes and silently falls
  back to sequential.
- **`cdk.json` says `python app.py`**, which resolves against whatever `python` is
  first on PATH. Invoke the CDK CLI so the app runs on the venv interpreter, or
  pass `--app "<sys.executable> app.py"`.

## Reading the results

`RESULTS.md` has one section per query class (needle, spread, trap, described)
with a row per arm and a paired bootstrap CI for P minus each Bedrock arm.

- **Never average across classes.** `described` is 25 of 89 queries and every mode
  scores under 0.23 on it, so an aggregate is set by that class alone.
- At 25 queries per class the 95% CI is roughly plus or minus 20 points, so only
  large effects are claimable.
- Queries whose evidence span no arm retrieved are flagged and excluded from every
  mean. Review them against the page image: the spans were originally validated
  against pdf-mcp's own extraction, so a span nobody finds may be a label defect.
