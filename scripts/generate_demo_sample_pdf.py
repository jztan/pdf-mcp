"""Generate the browser demo's sample PDF (pages/sample.pdf).

A fictional ~216-page services agreement with realistic multi-block
prose. Two properties matter and pull in opposite directions:

1. Key terms repeat across blocks *on the same page*, so the demo
   exercises the paragraph-block picker rather than a fixture where every
   phrase is unique to one page.
2. Key terms do NOT appear on *every* page. The document is divided into
   articles, each with its own vocabulary, so a search for "termination"
   lands on the handful of pages that are actually about termination.
   The earlier version put an identical recital containing the word
   "termination" on all 216 pages, which made the demo's search return
   every page in order and read as broken.

The recital appears once, on page 1, the way a real agreement is drafted.

Deterministic: fixed content, fixed metadata, no timestamps, no
randomness, so repeated runs produce identical page text.

Local development:
    uv run python scripts/generate_demo_sample_pdf.py pages/sample.pdf

CI runs this in deploy-demo.yml before the Pages artifact upload; the
output is gitignored and never committed.
"""

import sys
from pathlib import Path

import pymupdf

PAGE_COUNT = 216
SCANNED_PAGES = (40, 41, 42, 130)  # 0-indexed image-only pages
MAX_BYTES = 3 * 1024 * 1024

_PARTIES = [
    "Meridian Analytics Pte Ltd",
    "Northbridge Logistics Sdn Bhd",
    "the Service Provider",
    "the Customer",
]

# Each article owns a distinct vocabulary. `weight` sets how many pages
# the article spans, so article lengths are uneven the way a real
# contract's are; "Term and Termination" is deliberately short, which is
# what gives a search for "termination" a small, findable answer.
_ARTICLES: list[tuple[str, int, list[tuple[str, str]]]] = [
    (
        "Definitions and Interpretation",
        14,
        [
            (
                "Defined Terms",
                'For the purposes of this Agreement, "Deliverable" means any'
                " report, dataset, or software artefact identified in a"
                " Statement of Work. Defined terms carry the meaning given"
                " in this Article wherever they appear, and {a} shall"
                " construe any undefined term according to its ordinary"
                " commercial meaning.",
            ),
            (
                "Interpretation",
                "Headings in this Agreement are for convenience only and do"
                " not affect interpretation. References to a Schedule are"
                " references to a schedule of this Agreement, and any"
                " reference to a statute includes that statute as amended"
                " from time to time as it applies to {b}.",
            ),
            (
                "Order of Precedence",
                "Where a conflict arises between this Agreement and a"
                " Statement of Work, this Agreement prevails unless the"
                " Statement of Work expressly records that {a} and {b}"
                " intend to vary a specific defined term of this Agreement.",
            ),
        ],
    ),
    (
        "Scope of Services",
        20,
        [
            (
                "Statements of Work",
                "Each Statement of Work shall describe the scope, the"
                " Deliverables, the acceptance criteria, and the delivery"
                " schedule agreed between {a} and {b}. No Statement of Work"
                " takes effect until signed by an authorised representative"
                " of each party.",
            ),
            (
                "Change Control",
                "A change to the scope of a Statement of Work takes effect"
                " only through a written change request describing the"
                " change, its schedule impact, and its effect on the"
                " Service Fees. {a} shall respond to a change request"
                " within ten (10) Business Days.",
            ),
            (
                "Acceptance Testing",
                "{b} shall carry out acceptance testing of each Deliverable"
                " within fifteen (15) Business Days of delivery. A"
                " Deliverable is deemed accepted if {b} raises no written"
                " acceptance objection within that period.",
            ),
        ],
    ),
    (
        "Service Levels",
        18,
        [
            (
                "Availability Target",
                "The Service Provider shall maintain monthly availability of"
                " 99.9% measured across calendar months, excluding scheduled"
                " maintenance notified to {b} at least five (5) Business"
                " Days in advance. Availability is measured at the service"
                " boundary described in Schedule 3.",
            ),
            (
                "Service Credits",
                "Service credits accrue at five percent (5%) of monthly fees"
                " per full percentage point below the availability target,"
                " capped at thirty percent (30%) of the monthly fee. Service"
                " credits are {b}'s sole financial remedy for a failure to"
                " meet the availability target.",
            ),
            (
                "Incident Response",
                "The Service Provider shall acknowledge a Severity One"
                " incident within thirty (30) minutes and provide {b} with"
                " hourly status updates until the incident is resolved or"
                " downgraded in severity by agreement.",
            ),
        ],
    ),
    (
        "Fees, Invoicing and Payment",
        18,
        [
            (
                "Payment Terms",
                "All invoices are payable within thirty (30) days of receipt."
                " Late payment shall accrue interest at one and one-half"
                " percent (1.5%) per month. {a} may suspend Services if"
                " payment is more than sixty (60) days overdue, following"
                " written notice to {b}.",
            ),
            (
                "Invoicing",
                "The Service Provider shall invoice monthly in arrears"
                " against the fee schedule in Schedule 2. Each invoice shall"
                " itemise the Services delivered, the applicable rate, and"
                " any pass-through expenses approved in advance by {b}.",
            ),
            (
                "Disputed Invoices",
                "{b} may withhold payment of a disputed invoice line"
                " provided it pays the undisputed balance and notifies {a}"
                " of the basis of the dispute within fifteen (15) days of"
                " receipt of the invoice.",
            ),
        ],
    ),
    (
        "Confidentiality",
        16,
        [
            (
                "Confidential Information",
                "Each party shall protect the other's Confidential"
                " Information with no less than reasonable care, and shall"
                " not disclose it except to personnel of {a} with a need to"
                " know who are bound by obligations no less protective than"
                " this Article.",
            ),
            (
                "Permitted Disclosure",
                "Confidential Information may be disclosed where required by"
                " law or by a regulator of competent jurisdiction, provided"
                " that {a} gives {b} such prior notice of the required"
                " disclosure as is lawful and practicable.",
            ),
            (
                "Survival of Confidentiality",
                "The confidentiality obligations in this Article survive for"
                " five (5) years, and indefinitely in respect of any"
                " Confidential Information that constitutes a trade secret"
                " of {b} under applicable law.",
            ),
        ],
    ),
    (
        "Data Protection and Security",
        20,
        [
            (
                "Processing of Personal Data",
                "Where the Service Provider processes personal data on"
                " behalf of {b}, it acts as a processor and shall process"
                " that personal data only on documented instructions from"
                " {b}, save where required to do otherwise by applicable"
                " data protection law.",
            ),
            (
                "Security Measures",
                "The Service Provider shall encrypt personal data in transit"
                " and at rest, enforce multi-factor authentication for"
                " administrative access, and review its technical and"
                " organisational security measures at least annually.",
            ),
            (
                "Subprocessors",
                "The Service Provider shall not engage a subprocessor to"
                " process personal data without the prior written"
                " authorisation of {b}, and shall impose on any authorised"
                " subprocessor data protection obligations equivalent to"
                " those in this Article.",
            ),
        ],
    ),
    (
        "Intellectual Property",
        16,
        [
            (
                "Background IP",
                "Each party retains all right, title, and interest in its"
                " Background IP. Nothing in this Agreement transfers"
                " ownership of the Background IP of {a} to {b} or to any"
                " third party.",
            ),
            (
                "Deliverable IP",
                "On payment in full of the Service Fees for a Deliverable,"
                " {a} assigns to {b} all intellectual property rights in"
                " that Deliverable, excluding any Background IP embedded in"
                " it, which is licensed instead under Section 7.3.",
            ),
            (
                "Licence to Background IP",
                "{a} grants {b} a non-exclusive, perpetual, worldwide"
                " licence to use any Background IP embedded in a Deliverable"
                " to the extent necessary for {b} to use that Deliverable"
                " for its internal business purposes.",
            ),
        ],
    ),
    (
        "Warranties",
        14,
        [
            (
                "Service Warranty",
                "The Service Provider warrants that the Services will be"
                " performed with the reasonable skill and care expected of a"
                " competent supplier of comparable services, and that each"
                " Deliverable will conform in all material respects to its"
                " acceptance criteria.",
            ),
            (
                "Mutual Warranties",
                "Each party warrants that it has full corporate power to"
                " enter into this Agreement and that the person signing on"
                " behalf of {a} is duly authorised to bind it.",
            ),
            (
                "Warranty Exclusions",
                "The warranties in this Article do not apply where a defect"
                " arises from material supplied by {b}, from use of a"
                " Deliverable outside its documented operating envelope, or"
                " from modification of a Deliverable other than by {a}.",
            ),
        ],
    ),
    (
        "Indemnification",
        14,
        [
            (
                "IP Indemnity",
                "{a} shall indemnify {b} against any award of damages"
                " arising from a third-party claim that a Deliverable"
                " infringes that third party's intellectual property"
                " rights, provided {b} notifies {a} of the claim promptly"
                " and grants {a} conduct of the defence.",
            ),
            (
                "Data Breach Indemnity",
                "{a} shall indemnify {b} against regulatory fines and"
                " third-party claims arising from a breach of the security"
                " measures required under Article 6 that is attributable to"
                " {a}'s negligence.",
            ),
            (
                "Indemnity Conditions",
                "An indemnity under this Article is conditional on the"
                " indemnified party mitigating its loss and not admitting"
                " liability or settling a claim without the prior written"
                " consent of {a}, such consent not to be unreasonably"
                " withheld.",
            ),
        ],
    ),
    (
        "Limitation of Liability",
        14,
        [
            (
                "Liability Cap",
                "In no event shall either party's aggregate liability under"
                " this Agreement exceed the total Service Fees paid in the"
                " twelve (12) months preceding the claim. Nothing in this"
                " Article limits liability for gross negligence or willful"
                " misconduct of {a}.",
            ),
            (
                "Excluded Loss",
                "Neither party is liable for loss of profit, loss of"
                " anticipated savings, or any indirect or consequential"
                " loss, whether or not {b} advised {a} of the possibility of"
                " that loss at the Commencement Date.",
            ),
            (
                "Unlimited Liability",
                "The limitations in this Article do not apply to a party's"
                " liability for death or personal injury caused by its"
                " negligence, for fraud, or for {a}'s obligation to pay the"
                " Service Fees when due.",
            ),
        ],
    ),
    (
        "Insurance",
        10,
        [
            (
                "Required Cover",
                "The Service Provider shall maintain professional indemnity"
                " insurance of not less than five million dollars"
                " ($5,000,000) per claim throughout the Term and for six (6)"
                " years thereafter, with insurers of good repute acceptable"
                " to {b}.",
            ),
            (
                "Evidence of Insurance",
                "On written request, and no more than once per calendar"
                " year, {a} shall provide {b} with a certificate of currency"
                " evidencing each insurance policy required under this"
                " Article.",
            ),
            (
                "Insurance Not a Cap",
                "The insurance cover required under this Article does not"
                " limit the liability of {a} under this Agreement, and a"
                " failure of an insurer to pay does not relieve {a} of any"
                " obligation owed to {b}.",
            ),
        ],
    ),
    (
        "Force Majeure",
        8,
        [
            (
                "Force Majeure Event",
                "Neither party is liable for a failure to perform caused by"
                " a Force Majeure Event, being an event beyond its"
                " reasonable control including natural disaster, armed"
                " conflict, and the failure of a national"
                " telecommunications network relied on by {a}.",
            ),
            (
                "Mitigation",
                "A party affected by a Force Majeure Event shall notify {b}"
                " promptly, use reasonable endeavours to mitigate the"
                " effect of the event, and resume performance as soon as"
                " the Force Majeure Event ceases.",
            ),
            (
                "Prolonged Force Majeure",
                "Where a Force Majeure Event continues for more than sixty"
                " (60) consecutive days, either party may terminate the"
                " affected Statement of Work on written notice to {a},"
                " without liability other than for Services already"
                " performed.",
            ),
        ],
    ),
    (
        "Compliance and Audit",
        14,
        [
            (
                "Compliance with Law",
                "Each party shall comply with all applicable laws in"
                " performing this Agreement, including anti-bribery,"
                " sanctions, and export control laws applicable to {a} in"
                " the jurisdictions in which the Services are delivered.",
            ),
            (
                "Audit Rights",
                "{b} may audit {a}'s compliance with this Agreement no more"
                " than once per calendar year, on thirty (30) days' written"
                " notice, during normal business hours, and subject to the"
                " confidentiality obligations in Article 5.",
            ),
            (
                "Audit Findings",
                "Where an audit identifies a material non-compliance, {a}"
                " shall remediate it within thirty (30) days and shall bear"
                " the reasonable cost of the audit and of one follow-up"
                " audit confirming remediation by {b}.",
            ),
        ],
    ),
    # Deliberately short: this is the article a demo search for
    # "termination" is meant to find.
    (
        "Term and Termination",
        5,
        [
            (
                "Termination for Convenience",
                "Either party may terminate this Agreement upon ninety (90)"
                " days' prior written notice to the other party."
                " Termination under this Section shall not relieve {a} of"
                " its obligation to pay all Service Fees accrued prior to"
                " the effective date of termination.",
            ),
            (
                "Termination for Cause",
                "Either party may terminate this Agreement with immediate"
                " effect where {b} commits a material breach that is not"
                " remedied within thirty (30) days of written notice, or"
                " where {b} becomes insolvent. Termination for cause is"
                " without prejudice to accrued rights.",
            ),
            (
                "Effect of Termination",
                "On termination, {a} shall cease performing the Services,"
                " return or destroy Confidential Information, and invoice"
                " {b} for Services performed up to the effective date of"
                " termination. The Articles listed in Schedule 4 survive"
                " termination.",
            ),
        ],
    ),
    (
        "Dispute Resolution",
        7,
        [
            (
                "Escalation",
                "A dispute shall first be escalated to a nominated senior"
                " representative of each party, who shall meet within ten"
                " (10) Business Days of a written escalation notice served"
                " by {a} on {b}.",
            ),
            (
                "Arbitration",
                "A dispute not resolved by escalation shall be finally"
                " settled by arbitration seated in Singapore under the SIAC"
                " Rules, before a single arbitrator appointed by agreement"
                " between {a} and {b}.",
            ),
            (
                "Governing Law",
                "This Agreement is governed by the laws of Singapore, and"
                " each party submits to the exclusive jurisdiction of the"
                " Singapore courts in respect of any matter not required to"
                " be arbitrated under this Article.",
            ),
        ],
    ),
    (
        "General Provisions",
        7,
        [
            (
                "Notices",
                "A notice under this Agreement must be in writing and"
                " delivered to the address recorded for {b} in Schedule 1."
                " A notice sent by email is effective only where this"
                " Agreement expressly permits email notice.",
            ),
            (
                "Assignment",
                "Neither party may assign this Agreement without the prior"
                " written consent of the other, save that {a} may assign to"
                " an affiliate or to a successor of substantially the whole"
                " of its business on written notice to {b}.",
            ),
            (
                "Entire Agreement",
                "This Agreement, together with its Schedules and each"
                " Statement of Work, constitutes the entire agreement"
                " between {a} and {b} and supersedes all prior"
                " representations other than any made fraudulently.",
            ),
        ],
    ),
]

_RECITAL = (
    'This Master Services Agreement (the "Agreement") is entered into'
    " by and between {a} and {b}, effective as of the Commencement Date"
    " set out in Schedule 1, and governs all Statements of Work executed"
    " hereunder. The Articles that follow set out the scope of the"
    " Services, the Service Levels, the Service Fees, and the rights of"
    " each party on expiry or termination."
)

_PREAMBLE = (
    "WHEREAS {a} carries on the business of supplying data analytics and"
    " managed platform services; and WHEREAS {b} wishes to procure those"
    " services on the terms of this Agreement; NOW THEREFORE the parties"
    " agree as follows."
)

_MARGIN = 56
_PAGE_RECT = pymupdf.paper_rect("a4")


def _page_map() -> list[tuple[int, str, list[tuple[str, str]]]]:
    """One entry per content page: (article_number, title, clauses).

    Article weights are scaled to fill PAGE_COUNT - 1 pages (page 1 is
    the recital), with any rounding remainder given to the longest
    article so the total is exact and stable.
    """
    content_pages = PAGE_COUNT - 1
    total_weight = sum(w for _, w, _ in _ARTICLES)
    spans = [max(1, round(w * content_pages / total_weight)) for _, w, _ in _ARTICLES]

    # Absorb the rounding remainder into the longest article.
    drift = content_pages - sum(spans)
    spans[spans.index(max(spans))] += drift

    pages: list[tuple[int, str, list[tuple[str, str]]]] = []
    for idx, ((title, _, clauses), span) in enumerate(zip(_ARTICLES, spans)):
        pages.extend([(idx + 1, title, clauses)] * span)
    return pages


def _page_blocks(page_index: int, page_map: list) -> list[str]:
    """Three prose blocks per page, all from that page's article.

    Terms still repeat across the blocks of a single page (that is what
    the paragraph-block picker needs), but they no longer repeat across
    the whole document.
    """
    if page_index == 0:
        return [
            _RECITAL.format(a=_PARTIES[0], b=_PARTIES[1]),
            _PREAMBLE.format(a=_PARTIES[0], b=_PARTIES[1]),
            "Article 1 Definitions and Interpretation. Capitalised terms"
            " used in this Agreement have the meanings given in Article 1."
            " Each Article is numbered sequentially and cross-references"
            " within an Article are to Sections of that Article.",
        ]

    article_no, title, clauses = page_map[page_index - 1]
    # Section number restarts within each article, so numbering reads the
    # way a contract's does rather than running to §216.
    first_page_of_article = next(
        i for i, entry in enumerate(page_map) if entry[0] == article_no
    )
    section = (page_index - 1) - first_page_of_article + 1

    heading = f"Article {article_no} {title}"
    if section > 1:
        heading += " (continued)"
    blocks = [heading + "."]
    for j in range(3):
        # Rotate the starting clause per page so consecutive pages of the
        # same article are not byte-identical.
        clause_title, body = clauses[(page_index + j) % len(clauses)]
        a = _PARTIES[(page_index + j) % len(_PARTIES)]
        b = _PARTIES[(page_index + j + 1) % len(_PARTIES)]
        text = body.format(a=a, b=b)
        blocks.append(f"§{article_no}.{section}.{j + 1} {clause_title}. {text} {text}")
    return blocks


def _scanned_pixmap() -> pymupdf.Pixmap:
    """A small deterministic grayscale 'scan' texture."""
    width, height = 120, 168
    samples = bytes(
        180 + ((x * 7 + y * 13) % 40) for y in range(height) for x in range(width)
    )
    return pymupdf.Pixmap(pymupdf.csGRAY, width, height, samples, False)


def generate(out_path: Path) -> None:
    page_map = _page_map()
    doc = pymupdf.open()
    try:
        pix = _scanned_pixmap()
        for i in range(PAGE_COUNT):
            page = doc.new_page(width=_PAGE_RECT.width, height=_PAGE_RECT.height)
            if i in SCANNED_PAGES:
                page.insert_image(
                    pymupdf.Rect(
                        _MARGIN,
                        _MARGIN,
                        _PAGE_RECT.width - _MARGIN,
                        _PAGE_RECT.height - _MARGIN,
                    ),
                    pixmap=pix,
                )
                continue
            rect = pymupdf.Rect(
                _MARGIN,
                _MARGIN,
                _PAGE_RECT.width - _MARGIN,
                _PAGE_RECT.height - _MARGIN,
            )
            page.insert_textbox(
                rect,
                "\n\n".join(_page_blocks(i, page_map)),
                fontsize=10,
                fontname="helv",
                align=pymupdf.TEXT_ALIGN_JUSTIFY,
            )
        doc.set_metadata(
            {
                "title": "Master Services Agreement (Sample)",
                "author": "pdf-mcp demo",
                "creationDate": "D:20260101000000Z",
                "modDate": "D:20260101000000Z",
            }
        )
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()

    size = out_path.stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"sample.pdf is {size} bytes, exceeds budget of {MAX_BYTES}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pages/sample.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    generate(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
