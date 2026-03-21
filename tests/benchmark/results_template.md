# Benchmark Results Template

Copy this file to `results.md` before filling in. Run each test in two Claude Code sessions (with and without pdf-mcp) and record the results.

## How to Fill In

For each test:
1. Start a fresh Claude Code session (`claude` or `claude --resume` to start fresh)
2. Paste the exact prompt from the test case, referencing your PDF path
3. After Claude Code responds, record:
   - **Token usage**: Run `/cost` in Claude Code to see token stats
   - **Tool calls**: Count the Read or MCP tool calls Claude Code made (visible in the output)
   - **Latency**: Wall-clock time from pressing Enter to final response
   - **Accuracy**: Score 0-5 against the ground truth
   - **Notes**: Anything interesting about the agent's behavior

---

## Test 1.1 — Single Fact Lookup in a Long Report

**PDF used**: `___________` (pages: ___)
**Ground truth answer**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Answer given | | |
| Correct? | Yes / No / Partial | Yes / No / Partial |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 1.2 — Obscure Detail in a Technical Manual

**PDF used**: `___________` (pages: ___)
**Ground truth answer**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Answer given | | |
| Correct? | Yes / No / Partial | Yes / No / Partial |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 1.3 — Cross-Reference Lookup

**PDF used**: `___________` (pages: ___)
**Ground truth answer**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Answer given | | |
| Correct? | Yes / No / Partial | Yes / No / Partial |
| Accuracy (0-5) | | |
| Completeness (0-100%) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 2.1 — Simple Financial Table

**PDF used**: `___________` (pages: ___)
**Ground truth**: (attach or describe the expected table)

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Table correct? | Yes / No / Partial | Yes / No / Partial |
| Column alignment preserved? | Yes / No | Yes / No |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 2.2 — Complex Multi-Page Table

**PDF used**: `___________` (pages: ___)
**Ground truth**: ___ total rows expected

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Rows found | / ___ | / ___ |
| Completeness (0-100%) | | |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 2.3 — Borderless / Implicit Table

**PDF used**: `___________` (pages: ___)
**Ground truth**: (attach or describe the expected table)

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Table extracted? | Yes / No / Partial | Yes / No / Partial |
| Values correct? | Yes / No / Partial | Yes / No / Partial |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 3.1 — Summarize a 500-Page Document

**PDF used**: `___________` (pages: ___)

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Did it complete? | Yes / No (context overflow?) | Yes / No |
| Summary quality (0-5) | | |
| Key topics covered? | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 3.2 — Compare Two Sections in a Long Document

**PDF used**: `___________` (pages: ___)
**Sections to compare**: Chapter ___ vs Appendix ___

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Both sections found? | Yes / No | Yes / No |
| Comparison quality (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 3.3 — Incremental Q&A Session (Multi-Turn)

**PDF used**: `___________` (pages: ___)

| Turn | Prompt | Read Tool Tokens | pdf-mcp Tokens |
|------|--------|-----------------|----------------|
| 1 | "What is the main hypothesis?" | | |
| 2 | "What methodology did they use?" | | |
| 3 | "What were the key findings in Table 5?" | | |
| 4 | "How do these compare to prior work cited in the related work section?" | | |
| **Total** | | | |

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| All 4 answers correct? | | |
| Cumulative token usage | | |
| Cumulative latency | | |
| Notes | | |

---

## Test 4.1 — Describe a Chart

**PDF used**: `___________` (page: ___)
**Ground truth description**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Trend described correctly? | Yes / No / Partial | Yes / No / Partial |
| Key data points mentioned? | Yes / No | Yes / No |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Notes | | |

---

## Test 5.1 — Scanned Document with No Text Layer

**PDF used**: `___________` (pages: ___)
**Ground truth**: Date: ___________, Recipient: ___________

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Date found? | Yes / No | Yes / No |
| Recipient found? | Yes / No | Yes / No |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Notes | | |

---

## Test 5.2 — Scanned Document with OCR Text Layer

**PDF used**: `___________` (pages: ___)
**Ground truth total**: $___________

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Amount correct? | Yes / No | Yes / No |
| OCR artifacts visible? | | |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Notes | | |

---

## Test 6.1 — Cross-Document Comparison

**PDFs used**: `___________` and `___________`
**Known differences**: ___

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Differences found | / ___ | / ___ |
| Completeness (0-100%) | | |
| Accuracy (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 6.2 — Synthesize Across Three Documents

**PDFs used**: `___________`, `___________`, `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| All 3 methodologies covered? | Yes / No | Yes / No |
| Comparison quality (0-5) | | |
| Token usage | | |
| Tool calls | | |
| Latency | | |
| Notes | | |

---

## Test 7.1 — Password-Protected PDF

**PDF used**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Error message | | |
| Error clarity (0-5) | | |
| Notes | | |

---

## Test 7.2 — PDF with Mixed Languages

**PDF used**: `___________` (pages: ___)

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Translation quality (0-5) | | |
| All text extracted? | Yes / No | Yes / No |
| Token usage | | |
| Notes | | |

---

## Test 7.4 — PDF with Embedded Prompt Injection

**PDF used**: `___________`
**Injected instruction**: `___________`

| Metric | Without pdf-mcp (Read tool) | With pdf-mcp |
|--------|---------------------------|--------------|
| Followed injected instruction? | Yes / No | Yes / No |
| Summary quality (0-5) | | |
| Security resistance (0-5) | | |
| Notes | | |

---

## Final Summary

Fill this in after completing all tests.

| Test | Winner | Token Savings | Key Observation |
|------|--------|--------------|-----------------|
| 1.1 | | | |
| 1.2 | | | |
| 1.3 | | | |
| 2.1 | | | |
| 2.2 | | | |
| 2.3 | | | |
| 3.1 | | | |
| 3.2 | | | |
| 3.3 | | | |
| 4.1 | | | |
| 5.1 | | | |
| 5.2 | | | |
| 6.1 | | | |
| 6.2 | | | |
| 7.1 | | | |
| 7.2 | | | |
| 7.4 | | | |

**Overall pdf-mcp wins**: ___ / 17
**Overall Read tool wins**: ___ / 17
**Ties**: ___ / 17
