#!/usr/bin/env node
// Extracts the marker-delimited fusion block from pages/index.html and
// diffs its output against Python-generated fixtures. Exit 0 = parity.
// Run: node scripts/check_demo_fusion_parity.mjs
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("../pages/index.html", import.meta.url), "utf8");
const m = html.match(/\/\* __PDF_MCP_CORPUS_FUSION_BEGIN__ \*\/([\s\S]*?)\/\* __PDF_MCP_CORPUS_FUSION_END__ \*\//);
if (!m) { console.error("FAIL: fusion block markers not found in pages/index.html"); process.exit(1); }
const F = new Function(`${m[1]}\nreturn corpusFusion;`)();
const fx = JSON.parse(readFileSync(new URL("../tests/data/demo_fusion_fixtures.json", import.meta.url), "utf8"));

let fails = 0;
const fail = (name, got, want) => { fails++; console.error(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); };
const eqJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const toScores = (obj) => obj ? new Map(Object.entries(obj)) : null;

for (const c of fx.query_terms) {
  const got = [...F.corpusQueryTerms(c.query)].sort();
  if (!eqJson(got, c.expected)) fail(`query_terms(${c.query})`, got, c.expected);
}

for (const name of ["rrf_no_scores", "rrf_scores_tiebreak", "rrf_topk"]) {
  const c = fx[name];
  const got = F.rrfFuseDocRankings(c.rank_lists, F.CORPUS_RRF_K, c.top_k ?? null, toScores(c.scores));
  if (!eqJson(got, c.expected)) fail(name, got, c.expected);
}

{
  const c = fx.coverage_scores;
  const covered = new Map(Object.entries(c.covered).map(([p, t]) => [p, new Set(t)]));
  const got = F.corpusCoverageScores(covered);
  for (const [p, want] of Object.entries(c.expected)) {
    const g = got.get(p);
    if (g === undefined || Math.abs(g - want) > 1e-12) fail(`coverage_scores[${p}]`, g, want);
  }
}

{
  const c = fx.end_to_end;
  const got = F.rrfFuseDocRankings(c.rank_lists, F.CORPUS_RRF_K, 10, toScores(c.scores));
  if (!eqJson(got, c.expected_fused)) fail("end_to_end", got, c.expected_fused);
  // Permutation invariance: input order of documents must not matter.
  const perms = [[...c.rank_lists].reverse(), [c.rank_lists[c.rank_lists.length - 1], ...c.rank_lists.slice(0, -1)]];
  perms.forEach((perm, i) => {
    const g = F.rrfFuseDocRankings(perm, F.CORPUS_RRF_K, 10, toScores(c.scores));
    if (!eqJson(g, c.expected_fused)) fail(`permutation_invariance[${i}]`, g, c.expected_fused);
  });
  // docCoveredTerms round-trip on the raw texts.
  const terms = F.corpusQueryTerms(c.query);
  for (const [path, pages] of Object.entries(c.docs)) {
    const hitsForDoc = c.rank_lists.find((l) => l[0][0] === path);
    if (!hitsForDoc) continue;
    const texts = hitsForDoc.map(([, p]) => pages[String(p)]);
    const covered = F.docCoveredTerms(texts, terms);
    const docScore = toScores(c.scores).get(`${path}\n${hitsForDoc[0][1]}`);
    if (covered.size === 0 && docScore > 0) fail(`docCoveredTerms(${path})`, [...covered], "non-empty (score > 0)");
  }
}

if (fails) { console.error(`${fails} failure(s)`); process.exit(1); }
console.log("parity OK");
