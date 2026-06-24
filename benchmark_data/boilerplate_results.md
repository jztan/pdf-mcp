# Boilerplate (running header/footer) detection benchmark

F1 of per-block boilerplate classification on a synthetic corpus where headers/footers/page-numbers are injected at known positions, so labels are exact (`scripts/archive/benchmark_boilerplate.py`, `benchmark_data/boilerplate_corpus.json`). Each row adds one refinement; `freq_runs` is the full proposed method. `clean_control` and `numeric_body` are precision guardrails — the detector must strip nothing real there.

## F1 by scenario

| variant | simple | page_number_only | odd_even | multiline | section_scoped | clean_control | numeric_body | macro |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| naive_regex | 0.67 | 1.00 | 0.67 | 0.00 | 1.00 | 1.00 | 0.50 | 0.69 |
| freq_v0 | 0.67 | 0.00 | 0.00 | 0.67 | 0.00 | 1.00 | 0.00 | 0.33 |
| freq_bands | 0.67 | 0.00 | 0.00 | 0.67 | 0.00 | 1.00 | 0.00 | 0.33 |
| freq_digits | 1.00 | 1.00 | 0.67 | 1.00 | 0.67 | 1.00 | 1.00 | 0.90 |
| freq_parity | 1.00 | 1.00 | 1.00 | 1.00 | 0.67 | 1.00 | 1.00 | 0.95 |
| freq_runs | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

## Precision / recall — freq_runs (full proposed method)

| scenario | precision | recall | f1 |
| --- | --- | --- | --- |
| simple | 1.00 | 1.00 | 1.00 |
| page_number_only | 1.00 | 1.00 | 1.00 |
| odd_even | 1.00 | 1.00 | 1.00 |
| multiline | 1.00 | 1.00 | 1.00 |
| section_scoped | 1.00 | 1.00 | 1.00 |
| clean_control | 1.00 | 1.00 | 1.00 |
| numeric_body | 1.00 | 1.00 | 1.00 |

## Precision / recall — naive_regex (RAG-on-PDF-style baseline)

| scenario | precision | recall | f1 |
| --- | --- | --- | --- |
| simple | 1.00 | 0.50 | 0.67 |
| page_number_only | 1.00 | 1.00 | 1.00 |
| odd_even | 1.00 | 0.50 | 0.67 |
| multiline | 1.00 | 0.00 | 0.00 |
| section_scoped | 1.00 | 1.00 | 1.00 |
| clean_control | 1.00 | 1.00 | 1.00 |
| numeric_body | 0.33 | 1.00 | 0.50 |

## Caveats

- Synthetic corpus: text is digitally injected, so `freq_runs` scoring a clean 1.00 validates the algorithm's *logic and edge-case coverage*, not its robustness to OCR drift or near-duplicate text. Real scanned PDFs would score lower; fuzzy signature matching for OCR pages is the obvious next experiment (needs a real, network-fetched corpus).
- The variants are separated on purpose: `MIN_FRAC` is set above 0.5 so odd/even headers fail the document-wide test and must be earned by the parity rule. The takeaway is the *ordering* — each refinement recovers one failure mode at no precision cost on the guardrails.
