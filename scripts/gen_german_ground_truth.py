#!/usr/bin/env python
"""
scripts/gen_german_ground_truth.py

Derive ground truth for German-language retrieval quality — with no manual
annotation — from the BGB's own structure: every norm (`§ N Rubrik`) has its
own outline entry with a resolvable page, and the body text cites other
norms constantly ("... ein Widerrufsrecht gemaess Paragraph 355 zu ...").

Background: issue #46 (jztan/pdf-mcp) reported bge-small-en-v1.5 collapsing
on natural-language German semantic search (MRR 0.075 vs 0.266 for
bge-m3) using a private German commentary that cannot be shared. The
maintainer asked for the same style of benchmark on a document he can
download himself: https://www.gesetze-im-internet.de/bgb/BGB.pdf (490
pages, born-digital, no OCR). This script builds that ground truth.

Method (no LLM, no hand annotation):

1. Parse the PDF outline for `§ N Rubrik` entries -> {norm: (page, rubrik)}.
   Drop "(weggefallen)" (repealed) entries and "§§ N bis M" ranges.
2. Derive each norm's page extent from the next *norm* entry's page
   (structural headings are skipped when looking for "next"), capped at
   2 pages -- a longer gap usually means intervening entries were
   filtered out, so the extent is capped rather than assumed.
3. Validate the anchor: require the literal "§ N" and the rubric's first
   content word to both occur on the norm's own first page. This is a
   safety net against outline entries with an off-by-one page pointer.
4. Harvest every "§ N" occurrence elsewhere in the body ("referrers") --
   the pool of citing pages for the natural-language arm.
5. Sample norms with a seeded RNG from the norms that have a referrer.

Two scenarios per sampled norm ("arms"):

- `de<i>k` (keyword_control): the rubric verbatim. Deliberately the easy
  arm -- if keyword search doesn't find this, the corpus itself is broken,
  not the embedding model.
- `de<i>n` (semantic_xref): built from a referrer page's citing sentence,
  with every "§", "Abs./Absatz/Satz/Nr./Nummer" token and bare digit
  stripped out, so the query carries no verbatim citation for keyword
  search to win on for free. Wrapped in a fixed German question frame.

Output: benchmark_data/german_ground_truth.json, in the exact
ground_truth.json shape scripts/benchmark_embedding_models.py already
consumes (plus additive "k"/"arm"/"norm" keys the current harness code
ignores). The answer key pins the source PDF's sha256 directly on
pdfs.bgb.sha256; a sibling benchmark_data/german_ground_truth_provenance
.json duplicates it alongside the seed and skip counts by reason.
gesetze-im-internet.de republishes the BGB whenever the law changes,
silently shifting every page number, so before doing any real work this
script hashes the resolved PDF and compares it against the pin already
committed in --out: a mismatch exits 2 (not 1 -- reserved for "the
upstream document moved, regenerating would silently invalidate the
ground truth", distinct from a download failure) instead of producing
quietly-wrong page numbers. Pass --force to regenerate anyway.

Run:
    python scripts/gen_german_ground_truth.py
    python scripts/gen_german_ground_truth.py --n 90 --seed 20260913
    python scripts/gen_german_ground_truth.py --pdf /path/to/BGB.pdf
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pdf_mcp.docopen import open_pdf  # noqa: E402
from pdf_mcp.extractor import extract_text_from_page  # noqa: E402
from pdf_mcp.server import _resolve_path  # noqa: E402

try:
    from bench_env import environment
except ImportError:  # pragma: no cover - scripts/ always on sys.path when run
    environment = None  # type: ignore[assignment]

BGB_URL = "https://www.gesetze-im-internet.de/bgb/BGB.pdf"
_DEFAULT_OUT = "benchmark_data/german_ground_truth.json"

# Matches a single-norm outline entry: "§ 611a\xa0Arbeitsvertrag".
_ANCHOR_RE = re.compile(r"^§\s*(\d+[a-z]?)\s+(.+)$")
# Matches "§§ 3 bis 6" range entries -- skipped, not single norms.
_RANGE_RE = re.compile(r"^§§\s*\d+")
# Any "§ N" citation in body text.
_CITATION_RE = re.compile(r"§\s*(\d+[a-z]?)")
# A "§ N" immediately followed (after any Absatz/Satz/Nr chain) by
# "des/der <CapitalizedWord>...gesetz/-ordnung/-buch/-codes" cites a
# DIFFERENT statute, not the BGB -- e.g. "§ 109 der Zivilprozessordnung",
# "§ 25 Absatz 1 Satz 2 des Schwangerschaftskonfliktgesetzes". Resolving
# such a citation against the BGB's own norm numbers would mislabel it.
_EXTERNAL_STATUTE_RE = re.compile(
    r"^\s*(?:(?:Abs(?:atz)?\.?|Satz|Nr\.?|Nummer)\s*\d+[a-z]?\s*)*"
    r"(?:des|der)\s+[A-ZÄÖÜ][\wäöüß]*"
    r"(?:gesetz\w*|ordnung\w*|buch\w*|codes?|konvention\w*)\b"
)
# Citation-adjacent tokens to strip from a natural-language query.
_CITATION_TOKEN_RE = re.compile(
    r"§+\s*\d+[a-z]?"
    r"(?:\s*(?:Abs(?:atz)?\.?|Satz|Nr\.?|Nummer)\s*\d+[a-z]?)*"
    r"|\b(?:Abs(?:atz)?\.?|Satz|Nr\.?|Nummer)\b"
    r"|\b\d+[a-z]?\b",
    re.IGNORECASE,
)
# gesetze-im-internet.de prints these two lines on every single page; they
# are not part of the statute's text and would otherwise leak into a
# clause whose citation sits near the top of a page. BGB-specific, but
# harmless on any other PDF since the strings won't occur there.
_PAGE_BOILERPLATE_RE = re.compile(
    r"Ein Service des Bundesministerium.*?www\.gesetze-im-internet\.de\s*"
    r"|-\s*Seite\s+\d+\s+von\s+\d+\s*-",
    re.IGNORECASE | re.DOTALL,
)
# Function words that make a poor trailing token once the citation they
# governed ("nach § 143", "im Sinne des § 312") has been stripped out.
# Stripped iteratively from the end so "im Sinne des" collapses fully.
_TRAILING_STOPWORDS = {
    "nach",
    "gemäß",
    "gemaess",
    "entsprechend",
    "des",
    "der",
    "die",
    "das",
    "dem",
    "den",
    "von",
    "vom",
    "zu",
    "zum",
    "zur",
    "über",
    "auf",
    "in",
    "im",
    "bei",
    "unter",
    "sinne",
    "für",
    "und",
    "oder",
    "sowie",
    "als",
    "dass",
    "so",
}

# Sentence-boundary split for extract_clause(). A plain (?<=[.!?])\s+ treats
# an abbreviation's period as a sentence end, truncating the clause right
# before a citation like "...gemäß Abs. 2 § 355" -- exactly where German
# legal text abbreviates. Negative lookbehinds guard the abbreviations this
# module's own _CITATION_TOKEN_RE/_TRAILING_STOPWORDS already special-case
# as citation-adjacent, plus a few other common German legal abbreviations.
_SENTENCE_SPLIT_RE = re.compile(
    r"(?<!Abs\.)(?<!Nr\.)(?<!Art\.)(?<!Var\.)(?<!bzw\.)(?<!ggf\.)(?<=[.!?])\s+"
)

QUESTION_FRAMES = [
    "Was gilt für {clause}?",
    "Wo ist {clause} geregelt?",
]

MIN_CLAUSE_TOKENS = 5
MAX_EXTENT_PAGES = 2
CONTEXT_WINDOW_TOKENS = 14


@dataclass(frozen=True)
class Norm:
    number: str
    rubric: str
    start_page: int  # 1-indexed
    end_page: int  # 1-indexed, inclusive


@dataclass
class Referrer:
    page: int  # 1-indexed, citing page
    norm: str
    char_start: int
    char_end: int


@dataclass
class SkipLog:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1


def _clean_title(title: str) -> str:
    return title.replace("\xa0", " ").strip()


def parse_anchors(
    toc: list[list[Any]], skips: SkipLog | None = None
) -> dict[str, tuple[int, str]]:
    """Parse `§ N Rubrik` outline entries into {norm: (page, rubric)}.

    toc: [[level, title, page], ...] as returned by Document.get_toc()
    (1-indexed pages). Entries are kept in outline order; a later
    duplicate norm number overwrites an earlier one (outline lists each
    norm once in practice, but this keeps parsing total).
    """
    skips = skips or SkipLog()
    anchors: dict[str, tuple[int, str]] = {}
    for _level, raw_title, page in toc:
        title = _clean_title(str(raw_title))
        if _RANGE_RE.match(title):
            skips.add("range_entry")
            continue
        m = _ANCHOR_RE.match(title)
        if not m:
            continue  # structural entry (Buch/Abschnitt/Titel/...), not a norm
        rubric = m.group(2)
        if "(weggefallen)" in rubric.lower():
            skips.add("weggefallen")
            continue
        if page is None or page < 1:
            skips.add("unresolved_page")
            continue
        anchors[m.group(1)] = (int(page), rubric.strip())
    return anchors


def derive_extents(
    toc: list[list[Any]],
    anchors: dict[str, tuple[int, str]],
    skips: SkipLog | None = None,
) -> dict[str, Norm]:
    """Attach an end_page to each anchor from the immediately following
    *norm* entry's page (structural Buch/Abschnitt/Titel headings are
    skipped when looking for "next", since they are sparse and would
    otherwise make an ordinary norm near the end of a Buch look like it
    spans dozens of pages).

    The extent is capped at MAX_EXTENT_PAGES: a norm never claims more
    than one page beyond its own heading, even if the next norm entry is
    much further away (a large gap usually means intervening
    "(weggefallen)" entries were filtered out, so the true end of this
    norm's own text is unknown -- claiming it anyway would mis-score
    rather than protect the benchmark, so it is capped, not assumed).
    """
    skips = skips or SkipLog()

    # Norm entries in outline order, keeping only each number's winning
    # occurrence (matches parse_anchors' last-occurrence-wins rule).
    norm_positions: list[tuple[str, int]] = []
    for _level, raw_title, page in toc:
        title = _clean_title(str(raw_title))
        if _RANGE_RE.match(title):
            continue
        m = _ANCHOR_RE.match(title)
        if not m:
            continue
        number = m.group(1)
        if number in anchors and anchors[number][0] == page:
            norm_positions.append((number, int(page)))

    # A duplicate norm number's *winning* (last) occurrence can sit at an
    # earlier TOC position than another norm's own entry (e.g. §6 at index
    # 1, §5's winning duplicate at index 2 after an earlier §5 was
    # superseded) -- outline order and page order agree everywhere else,
    # but not there. Sort by page (stable, so same-page entries keep their
    # outline order) so "next" always means "next by page", not "next
    # surviving TOC index".
    norm_positions.sort(key=lambda np: np[1])

    norms: dict[str, Norm] = {}
    for i, (number, start_page) in enumerate(norm_positions):
        rubric = anchors[number][1]
        next_page = (
            norm_positions[i + 1][1] if i + 1 < len(norm_positions) else start_page
        )
        gap = max(next_page - start_page, 0)
        end_page = start_page + min(gap, MAX_EXTENT_PAGES - 1)
        norms[number] = Norm(
            number=number, rubric=rubric, start_page=start_page, end_page=end_page
        )
    return norms


def validate_anchor(norm: Norm, page_text: str, skips: SkipLog | None = None) -> bool:
    """Confirm the norm's own citation and rubric both occur on its start page."""
    skips = skips or SkipLog()
    normalized = re.sub(r"\s+", " ", page_text.replace("\xa0", " "))
    # Word-boundary match: plain substring containment would let norm "9"
    # pass on a page whose only citation is "§ 90", or norm "61" pass on
    # a page whose only citation is "§ 611a" -- neither actually cites
    # this norm. (?![0-9a-z]) rejects a match immediately followed by
    # another digit or the start of a letter suffix.
    citation_re = re.compile(r"§\s*" + re.escape(norm.number) + r"(?![0-9a-z])")
    if not citation_re.search(normalized):
        skips.add("citation_not_on_own_page")
        return False
    first_word = norm.rubric.split()[0].rstrip(";,.") if norm.rubric.split() else ""
    if first_word and first_word not in normalized:
        skips.add("rubric_not_on_own_page")
        return False
    return True


def harvest_referrers(
    page_texts: dict[int, str],
    anchors: dict[str, tuple[int, str]],
    skips: SkipLog | None = None,
) -> dict[str, list[Referrer]]:
    """Find every `§ N` citation on a page other than N's own anchor page.

    Skips a citation that is immediately followed by "des/der <a different
    statute's name>" (e.g. "§ 109 der Zivilprozessordnung") -- the BGB
    cites other codes constantly, and resolving that "§ N" against the
    BGB's own norm numbers would mislabel the scenario.
    """
    skips = skips or SkipLog()
    referrers: dict[str, list[Referrer]] = {}
    for page, text in page_texts.items():
        for m in _CITATION_RE.finditer(text):
            number = m.group(1)
            anchor = anchors.get(number)
            if anchor is None or anchor[0] == page:
                continue
            lookahead = text[m.end() : m.end() + 80]
            if _EXTERNAL_STATUTE_RE.match(lookahead):
                skips.add("external_statute_citation")
                continue
            referrers.setdefault(number, []).append(
                Referrer(page=page, norm=number, char_start=m.start(), char_end=m.end())
            )
    return referrers


def extract_clause(text: str, char_start: int) -> str | None:
    """Return the sentence/clause immediately before a citation, cleaned.

    Cuts at the nearest sentence boundary, then caps to the last
    CONTEXT_WINDOW_TOKENS tokens so a very long lead-in doesn't dominate.
    """
    before = text[:char_start]
    sentences = _SENTENCE_SPLIT_RE.split(before)
    clause = sentences[-1] if sentences else before
    tokens = clause.split()
    if len(tokens) > CONTEXT_WINDOW_TOKENS:
        tokens = tokens[-CONTEXT_WINDOW_TOKENS:]
    clause = " ".join(tokens).strip(" ,;:")
    return clause or None


def strip_citation_tokens(clause: str) -> str:
    stripped = _CITATION_TOKEN_RE.sub("", clause)
    # A citation like "(§ 143)" leaves an empty "()" behind once the token
    # inside is stripped; drop empty/punctuation-only parenthetical shells.
    stripped = re.sub(r"\(\s*[,;:]*\s*\)", "", stripped)
    stripped = re.sub(r"\s+([,.;:])", r"\1", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" ,;:()")
    # The citation was often the object of a trailing preposition/article
    # ("nach", "im Sinne des"); with it gone that preposition dangles at
    # the very end. Peel trailing stopwords one at a time (bounded so an
    # unrelated short clause can't be stripped down to nothing).
    words = stripped.split()
    peeled = 0
    while (
        words and words[-1].lower().strip(".,;:") in _TRAILING_STOPWORDS and peeled < 4
    ):
        words.pop()
        peeled += 1
    return " ".join(words).strip(" ,;:()")


def build_natural_query(clause: str, rng: random.Random, rubric: str) -> str | None:
    """Strip citation tokens and wrap the clause in a seeded question frame.

    Returns None if the result fails an anti-triviality check: still
    contains a digit/§, too short, or mostly restates the target rubric.
    """
    stripped = strip_citation_tokens(clause)
    if re.search(r"[§0-9]", stripped):
        return None
    content_tokens = [t for t in stripped.split() if len(t) > 2]
    if len(content_tokens) < MIN_CLAUSE_TOKENS:
        return None
    rubric_tokens = {t.lower() for t in rubric.split() if len(t) > 2}
    overlap = {t.lower() for t in content_tokens} & rubric_tokens
    if rubric_tokens and len(overlap) / len(rubric_tokens) > 0.5:
        return None
    frame = rng.choice(QUESTION_FRAMES)
    return frame.format(clause=stripped)


def build_keyword_query(rubric: str) -> str:
    """The rubric verbatim, trailing clause and citation numbers stripped."""
    head = rubric.split(";")[0].strip()
    tokens = head.split()[:6]
    return " ".join(tokens)


def stratify_by_buch(toc: list[list[Any]], norms: dict[str, Norm]) -> dict[str, str]:
    """Map each norm number to its enclosing "Buch" (Book) title, if found.

    Falls back to "unknown" for a norm before the first Buch heading (there
    should be none in the BGB, but this keeps sampling total for other
    German statutes with a different top-level structure).
    """
    buch_starts = sorted(
        (int(page), _clean_title(str(title)))
        for level, title, page in toc
        if level == 1 and _clean_title(str(title)).startswith("Buch") and page
    )
    mapping: dict[str, str] = {}
    for number, norm in norms.items():
        buch = "unknown"
        for start_page, title in buch_starts:
            if start_page <= norm.start_page:
                buch = title
            else:
                break
        mapping[number] = buch
    return mapping


def sample_norms(
    eligible: list[str],
    buch_of: dict[str, str],
    n: int,
    seed: int,
    force_include: list[str] | None = None,
) -> list[str]:
    """Seeded, Buch-stratified sample of n norm numbers from eligible."""
    rng = random.Random(seed)
    forced = [f for f in (force_include or []) if f in eligible]
    pool = [e for e in eligible if e not in forced]

    by_buch: dict[str, list[str]] = {}
    for number in pool:
        by_buch.setdefault(buch_of.get(number, "unknown"), []).append(number)
    for group in by_buch.values():
        rng.shuffle(group)

    remaining = max(n - len(forced), 0)
    buchs = sorted(by_buch)
    picked: list[str] = []
    idx = 0
    while remaining > 0 and buchs:
        buch = buchs[idx % len(buchs)]
        group = by_buch[buch]
        if group:
            picked.append(group.pop())
            remaining -= 1
        else:
            buchs.remove(buch)
            if not buchs:
                break
            continue
        idx += 1
    return forced + picked


def build_scenarios(
    sampled: list[str],
    norms: dict[str, Norm],
    referrers: dict[str, list[Referrer]],
    page_texts: dict[int, str],
    seed: int,
    skips: SkipLog,
) -> dict[str, dict]:
    rng = random.Random(seed)
    scenarios: dict[str, dict] = {}
    i = 0
    for number in sampled:
        norm = norms[number]
        relevant_pages = list(range(norm.start_page, norm.end_page + 1))
        natural_query = None
        refs = referrers.get(number, [])
        if not refs:
            skips.add("no_referrer")
        else:
            ref = rng.choice(refs)
            # Excluded window is exactly the norm's own (already-computed)
            # extent, not an arbitrary +/-1 buffer independent of it -- a
            # buffer that also excludes start_page-1 would throw away
            # legitimate referrer candidates one page before a norm's own
            # text even starts, for no protective reason; a wider extent
            # (MAX_EXTENT_PAGES raised later) widens this exclusion too,
            # automatically, since it reads norm.end_page directly.
            if norm.start_page <= ref.page <= norm.end_page:
                skips.add("referrer_too_close")
            else:
                page_text = page_texts.get(ref.page, "")
                clause = extract_clause(page_text, ref.char_start)
                if clause is None:
                    skips.add("empty_clause")
                else:
                    natural_query = build_natural_query(clause, rng, norm.rubric)
                    if natural_query is None:
                        skips.add("natural_query_rejected")

        if natural_query is None:
            # No usable xref query (e.g. a norm no other page cites, such
            # as a force-included one). Still emit the keyword-control
            # arm alone rather than dropping the norm silently.
            i += 1
            sid_base = f"de{i:02d}"
            scenarios[f"{sid_base}k"] = {
                "query": build_keyword_query(norm.rubric),
                "relevant_pages": relevant_pages,
                "k": 5,
                "arm": "keyword_control",
                "norm": number,
                "notes": (
                    f"auto-derived: § {number} rubric, keyword control arm "
                    "(no usable cross-reference for a semantic_xref pair)"
                ),
            }
            continue

        i += 1
        sid_base = f"de{i:02d}"
        scenarios[f"{sid_base}k"] = {
            "query": build_keyword_query(norm.rubric),
            "relevant_pages": relevant_pages,
            "k": 5,
            "arm": "keyword_control",
            "norm": number,
            "notes": f"auto-derived: § {number} rubric, keyword control arm",
        }
        # The query text is lifted verbatim from ref.page, so a model that
        # retrieves that page back is not wrong -- it found the sentence's
        # own context. Excluding ref.page from "relevant" would punish
        # exactly the retrieval a good German model should make. Include
        # it explicitly and keep the norm-only pages separately so a
        # reader can recompute the stricter scoring if they want it.
        xref_relevant_pages = sorted(set(relevant_pages) | {ref.page})
        scenarios[f"{sid_base}n"] = {
            "query": natural_query,
            "relevant_pages": xref_relevant_pages,
            "target_pages": relevant_pages,
            "referrer_page": ref.page,
            "k": 5,
            "arm": "semantic_xref",
            "norm": number,
            "notes": (
                f"auto-derived: § {number}, referrer p.{ref.page}, " f"seed={seed}"
            ),
        }
    return scenarios


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pinned_sha256(out_arg: str) -> str | None:
    """Read pdfs.bgb.sha256 from the currently committed answer key.

    Uses `out_arg` when it names a real file; falls back to the default
    ground-truth path when writing to stdout ('-'), so `--out -` still
    gets checked against whatever pin is currently committed instead of
    silently skipping the check.

    A missing file returns None (nothing committed yet -- proceed, there
    is nothing to protect). A file that exists but fails to parse raises
    instead: silently treating a corrupt answer key as "no pin, proceed"
    would defeat the whole point of pinning -- the one case this check
    exists for is "don't let a bad state through unnoticed".
    """
    path = Path(out_arg) if out_arg != "-" else Path(_DEFAULT_OUT)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{path} exists but is not valid JSON -- refusing to treat this "
            "as 'no pin committed yet'. Fix or remove it before rerunning."
        ) from e
    return data.get("pdfs", {}).get("bgb", {}).get("sha256")


def generate(
    pdf_path: str,
    n: int,
    seed: int,
    force_include: list[str] | None = None,
) -> tuple[dict, dict]:
    """Run the full pipeline against a real PDF. Returns (ground_truth, provenance)."""
    skips = SkipLog()
    doc = open_pdf(pdf_path)
    try:
        toc = doc.get_toc()
        page_count = len(doc)
        anchors = parse_anchors(toc, skips)
        norms = derive_extents(toc, anchors, skips)

        page_texts: dict[int, str] = {}
        for p in range(1, page_count + 1):
            raw = extract_text_from_page(doc[p - 1])
            page_texts[p] = _PAGE_BOILERPLATE_RE.sub(" ", raw)

        validated = {
            number: norm
            for number, norm in norms.items()
            if validate_anchor(norm, page_texts.get(norm.start_page, ""), skips)
        }
        referrers = harvest_referrers(page_texts, anchors, skips)
        eligible = sorted(number for number in validated if number in referrers)
        # A force-included norm may have no referrer anywhere in the body
        # (e.g. § 611a, cited by nothing else in the BGB) -- still let it
        # into the pool so sample_norms can include it; build_scenarios
        # falls back to the keyword-only arm for a norm with no referrer.
        for number in force_include or []:
            if number in validated and number not in eligible:
                eligible.append(number)
        eligible.sort()

        buch_of = stratify_by_buch(toc, validated)
        sampled = sample_norms(eligible, buch_of, n, seed, force_include)

        scenarios = build_scenarios(
            sampled, validated, referrers, page_texts, seed, skips
        )
    finally:
        close = getattr(doc, "close", None)
        if close:
            close()

    pdf_sha256 = _sha256(Path(pdf_path))
    ground_truth = {
        "pdfs": {
            "bgb": {
                "url": BGB_URL,
                "title": "Bürgerliches Gesetzbuch (BGB)",
                "page_count": page_count,
                "sha256": pdf_sha256,
                "scenarios": scenarios,
            }
        }
    }
    provenance = {
        "seed": seed,
        "sample_size": n,
        "script_version": 2,
        "sha256": pdf_sha256,
        "skip_counts": skips.counts,
        "eligible_norm_count": len(eligible),
        "sampled_norm_count": len(sampled),
        "scenario_count": len(scenarios),
    }
    if environment is not None:
        env = environment()
        # This provenance file is committed, unlike the gitignored
        # benchmark_results/ payloads the other scripts write: drop the
        # generating machine's interpreter path, which identifies a
        # developer's home directory and says nothing the recorded
        # python/platform versions don't.
        env.pop("executable", None)
        provenance["environment"] = env
    return ground_truth, provenance


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate German ground truth from the BGB's own structure."
    )
    parser.add_argument(
        "--n", type=int, default=90, help="Norms to sample (default: 90)"
    )
    parser.add_argument("--seed", type=int, default=20260913, help="RNG seed")
    parser.add_argument(
        "--pdf", default=None, help="Path to an already-downloaded BGB.pdf"
    )
    parser.add_argument(
        "--force-include",
        default="611a",
        help="Comma-separated norm numbers to always sample (default: 611a)",
    )
    parser.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help="Output path ('-' for stdout)",
    )
    parser.add_argument(
        "--provenance-out",
        default="benchmark_data/german_ground_truth_provenance.json",
        help="Provenance output path",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Regenerate even if the source PDF's sha256 no longer matches "
            "the pin committed in --out (page numbers may then be stale). "
            "Without this, a mismatch exits 2."
        ),
    )
    args = parser.parse_args()

    if args.pdf:
        pdf_path = args.pdf
    else:
        resolved, err = _resolve_path(BGB_URL)
        if err is not None:
            print(f"Failed to download {BGB_URL}: {err['error']}", file=sys.stderr)
            sys.exit(1)
        pdf_path = resolved

    try:
        pinned = _pinned_sha256(args.out)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if pinned and not args.force:
        actual = _sha256(Path(pdf_path))
        if actual != pinned:
            print(
                f"ERROR: BGB.pdf sha256 mismatch (pinned {pinned}, resolved "
                f"{actual}). The BGB was likely amended, which shifts page "
                "numbers throughout -- regenerating would silently "
                "invalidate every number measured against the committed "
                "ground truth. Pass --force to regenerate anyway.",
                file=sys.stderr,
            )
            sys.exit(2)

    force_include = (
        [f.strip() for f in args.force_include.split(",") if f.strip()]
        if args.force_include
        else []
    )
    gt, provenance = generate(pdf_path, args.n, args.seed, force_include)

    gt_json = json.dumps(gt, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.out == "-":
        sys.stdout.write(gt_json)
    else:
        out_path = Path(args.out)

        prev_hash = None
        if out_path.exists():
            try:
                prev_provenance = json.loads(
                    Path(args.provenance_out).read_text(encoding="utf-8")
                )
                prev_hash = prev_provenance.get("sha256")
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        if prev_hash and prev_hash != provenance["sha256"] and not args.force:
            # Belt-and-braces: the early pin check above already reads
            # pdfs.bgb.sha256 out of the same --out file before generate()
            # runs, so this should be unreachable in practice. Kept so the
            # provenance file's own sha256 (a second, independent source)
            # is never allowed to disagree with what gets written.
            print(
                f"ERROR: source PDF sha256 changed ({prev_hash} -> "
                f"{provenance['sha256']}). The BGB was likely amended, "
                "which shifts page numbers throughout -- regenerating would "
                "silently invalidate every number measured against the "
                f"committed ground truth. Refusing to overwrite {out_path}. "
                "Pass --force to regenerate anyway.",
                file=sys.stderr,
            )
            sys.exit(2)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(gt_json, encoding="utf-8")
        Path(args.provenance_out).write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"Wrote {len(gt['pdfs']['bgb']['scenarios'])} scenarios "
            f"({provenance['sampled_norm_count']} norms) to {out_path}",
            file=sys.stderr,
        )
        print(f"Skip counts: {provenance['skip_counts']}", file=sys.stderr)


if __name__ == "__main__":
    main()
