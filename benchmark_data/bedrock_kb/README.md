# Bedrock KB anchor benchmark: runbook

Compares pdf-mcp corpus search against Amazon Bedrock Knowledge Bases on the
existing 100-document `corpus_search` benchmark, scored by evidence-span
containment at an equal token budget, reported per query class.

Four arms: `P` (pdf-mcp, `excerpt_style="paragraph"`, the tool default),
`P-snippet` (identical retrieval, `excerpt_style="snippet"`), `B0` (Bedrock
default parser and chunking), `B1` (Bedrock fixed-1000 chunks plus Cohere
Rerank 3.5). `P` is the reference arm; every other arm is compared against
it with a paired bootstrap CI.

**Bedrock is an anchor, not a subject.** Every pdf-mcp number so far is pdf-mcp
compared to itself; this gives those numbers an outside reference point. Any
result is acceptable, including "pdf-mcp equals the anchor". There is no kill
condition and no thesis being tested.

Design and plan: an anchor comparison of pdf-mcp corpus search against two
Bedrock Knowledge Base configurations (default chunking, and fixed-1000
chunking plus Cohere rerank), scored by evidence-span containment at a fixed
token budget, per query class, with paired bootstrap CIs.
Branch: `feat/bedrock-kb-anchor`, worktree `/Users/jztan/src/pdf-mcp-bedrock-kb`

## State as of 2026-08-29

| Piece | File | Status |
|---|---|---|
| Corpus warm | (cache) | DONE, 100/100 docs, text + embeddings |
| Scoring harness | `scripts/benchmark_bedrock_kb.py` | DONE |
| Arm P adapter | `scripts/benchmark_bedrock_kb.py::run_arm_p` | DONE, runs live |
| CDK stacks | `infra/bedrock_kb/` | DONE, both deployed (`CREATE_COMPLETE`) |
| boto3 helpers | `scripts/_bedrock_kb.py` | DONE, stub-tested |
| Arm configs | `benchmark_data/bedrock_kb/config.json` | DONE, committed |
| Stack CLI | `scripts/bedrock_kb_stack.py` | DONE |
| Bedrock arm adapter | `retrieve` / `rerank` / `run_arm_bedrock` | DONE |
| Benchmark CLI + run | `main()` in the harness | DONE, full run complete |

Both stacks (`pdfmcp-anchor-b0-default-v1`, `pdfmcp-anchor-b1-fixed1000-v1`)
are deployed, ingested (100 scanned, 100 indexed, 0 failed on both), and
queried against the full 89-query set; results are in `RESULTS.md` and
`results.json`. Both stacks are retained deliberately (see "Keep the
indexes" below): idle cost is about $0.02/month combined. This is not a
zero-spend state, and it is not meant to be torn down after reading this
file.

## Prerequisites

```bash
cd /Users/jztan/src/pdf-mcp-bedrock-kb
uv sync --extra dev --extra bedrock
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1   # node 26 > jsii's tested range
aws sts get-caller-identity        # confirm the right account
aws configure get region           # must be us-east-1
```

Run the tests to confirm the tree is sound (82 tests, no AWS, no spend):

```bash
uv run pytest tests/test_benchmark_bedrock_kb.py tests/test_bedrock_kb_infra.py \
              tests/test_bedrock_kb_stack.py tests/test_bedrock_kb_cli.py -q
```

## How to re-run

Both stacks are already deployed and ingested; a re-run only needs the
benchmark CLI, not `deploy`/`upload`/`ingest` again:

```bash
uv run python scripts/benchmark_bedrock_kb.py
```

This runs both local arms live, retrieves from both Bedrock arms, and overwrites
`results.json` and `RESULTS.md` in this directory (never `ANALYSIS.md`,
which is hand-written; see its top-of-file note). The drift guard in
`main()` refuses to run if either stack's config tag or ingest stamp no
longer matches `config.json` or the corpus manifest.

To check a single arm's live status without querying it:

```bash
uv run python scripts/bedrock_kb_stack.py status --arm B0-default-v1
uv run python scripts/bedrock_kb_stack.py status --arm B1-fixed1000-v1
```

## If you need to rebuild from scratch

This only applies after a `destroy`, or for a new arm id. The account
already has a billing guardrail in place ("My Monthly Cost Budget",
$6/month, 90% and 100% alert thresholds) from before this project started;
there is no `pdfmcp-anchor`-specific budget and none is needed.

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

# One-time CDK bootstrap, if this account/region has never been bootstrapped
cd infra/bedrock_kb
npx aws-cdk@2 bootstrap "aws://$ACCOUNT/us-east-1"
cd ../..

# Per arm
uv run python scripts/bedrock_kb_stack.py deploy --arm <arm-id>
uv run python scripts/bedrock_kb_stack.py upload --arm <arm-id>
uv run python scripts/bedrock_kb_stack.py ingest --arm <arm-id>
```

**`ingest` refuses to stamp `ingested` unless its own statistics account
for every manifest document** (scanned and indexed both equal to the
manifest count, 0 failed): a silently skipped document would exist in arm
P but not in this index, and the gap would read as a retrieval failure
rather than an ingest failure. If it refuses, read `failureReasons` from
its printed output.

## Result

| class | P-para | P-snip | B0 | B1 | verdict |
|---|---|---|---|---|---|
| described | 0.120 | 0.000 | 0.360 | 0.400 | Bedrock ahead, CI excludes zero |
| needle | 0.429 | 0.643 | 0.714 | 0.714 | tie, every CI includes zero |
| spread | 0.640 | 0.880 | 0.600 | 0.680 | snippet beats paragraph, CI excludes zero |
| trap | 0.520 | 0.560 | 0.840 | 0.880 | Bedrock ahead, CI excludes zero |

Span recall, all 89 queries. See [ANALYSIS.md](ANALYSIS.md) for what these
mean, what does not survive the noise, and why span recall is not a retrieval
verdict.

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

`RESULTS.md` is generated (do not hand-edit; a re-run overwrites it) and has
one section per query class (needle, spread, trap, described), each with a
primary table (all 89 queries) and a labelled sensitivity table (flagged
queries excluded), plus a paired bootstrap CI for P minus each Bedrock arm
under both. **See [`ANALYSIS.md`](ANALYSIS.md) for the interpretation**,
including where the excerpt-selection story does and does not hold across
classes, the flagged-query review, provenance, and observed cost; that file
is hand-written and is not overwritten by a re-run.

- **Never average across classes.** `described` is 25 of 89 queries, and it is
  the weakest class for pdf-mcp's own retrieval modes (see
  `docs_internal/corpus-vs-single-doc-performance.md` and
  `benchmark_data/corpus_search/modes_results.md`), so an aggregate would be
  set by that one class. This is not true of every arm anchored here: B0 and
  B1 score 0.360 and 0.400 span recall on `described` in this run, well above
  0.23.
- At 25 queries per class the 95% CI is roughly plus or minus 20 points, so only
  large effects are claimable.
- Queries whose evidence span no arm retrieved are flagged, reviewed against
  the page image, and (per `ANALYSIS.md`) confirmed as genuine misses rather
  than label defects, so the primary tables include them as legitimate 0-0
  observations; the sensitivity tables exclude them, matching the original
  pre-review methodology. Both are reported; see `ANALYSIS.md`'s
  flagged-inclusion sensitivity check for why the choice does not change any
  conclusion.
