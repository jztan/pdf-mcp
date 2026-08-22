# Reading-order fidelity benchmark

Order score = sequence similarity of normalized token streams vs READoc ground truth (order-sensitive). Recall = order-insensitive token overlap vs the same ground truth. `pdfmcp` = current `extract_text_from_page`; `p4llm` = PyMuPDF4LLM column-aware path we ship; `reference` = external XY-cut reference (when provided).

## Aggregates

| group | n | pdfmcp_order | p4llm_order | ref_order | pdfmcp_recall | p4llm_recall | ref_recall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| two_column | 22 | 0.795 | n/a | n/a | 0.909 | n/a | n/a |
| one_column | 22 | 0.861 | n/a | n/a | 0.899 | n/a | n/a |

## Per-document

| id | group | pdfmcp_order | p4llm_order | ref_order |
| --- | --- | --- | --- | --- |
| 0707.1301 | two_column | 0.665 | n/a | n/a |
| 0709.4466 | two_column | 0.934 | n/a | n/a |
| 1207.2761 | two_column | 0.869 | n/a | n/a |
| 1301.3570 | two_column | 0.692 | n/a | n/a |
| 1302.3440 | two_column | 0.714 | n/a | n/a |
| 1302.4245 | two_column | 0.849 | n/a | n/a |
| 1307.7059 | two_column | 0.959 | n/a | n/a |
| 1401.4991 | two_column | 0.767 | n/a | n/a |
| 1406.4582 | two_column | 0.811 | n/a | n/a |
| 1406.6799 | two_column | 0.742 | n/a | n/a |
| 1409.7193 | two_column | 0.476 | n/a | n/a |
| 1501.05624 | two_column | 0.949 | n/a | n/a |
| 1601.06071 | two_column | 0.887 | n/a | n/a |
| 1606.06090 | two_column | 0.945 | n/a | n/a |
| 1612.09007 | two_column | 0.809 | n/a | n/a |
| 1712.00712 | two_column | 0.725 | n/a | n/a |
| 1807.03386 | two_column | 0.805 | n/a | n/a |
| 1807.11632 | two_column | 0.861 | n/a | n/a |
| 1808.03354 | two_column | 0.951 | n/a | n/a |
| 1808.08321 | two_column | 0.690 | n/a | n/a |
| 1811.03679 | two_column | 0.469 | n/a | n/a |
| 1910.03474 | two_column | 0.932 | n/a | n/a |
| 0705.4297 | one_column | 0.899 | n/a | n/a |
| 0706.0028 | one_column | 0.916 | n/a | n/a |
| 0706.0954 | one_column | 0.665 | n/a | n/a |
| 0706.2397 | one_column | 0.844 | n/a | n/a |
| 0707.0311 | one_column | 0.914 | n/a | n/a |
| 0707.3690 | one_column | 0.690 | n/a | n/a |
| 0707.4042 | one_column | 0.919 | n/a | n/a |
| 0709.2178 | one_column | 0.933 | n/a | n/a |
| 0709.2857 | one_column | 0.812 | n/a | n/a |
| 0710.2265 | one_column | 0.913 | n/a | n/a |
| 0710.2740 | one_column | 0.957 | n/a | n/a |
| 0711.0528 | one_column | 0.933 | n/a | n/a |
| 0711.3236 | one_column | 0.969 | n/a | n/a |
| 0802.0539 | one_column | 0.762 | n/a | n/a |
| 0802.0733 | one_column | 0.947 | n/a | n/a |
| 0811.0781 | one_column | 0.873 | n/a | n/a |
| 0811.0851 | one_column | 0.919 | n/a | n/a |
| 0902.1533 | one_column | 0.934 | n/a | n/a |
| 0903.1810 | one_column | 0.957 | n/a | n/a |
| 0904.1520 | one_column | 0.838 | n/a | n/a |
| 0905.2570 | one_column | 0.704 | n/a | n/a |
| 0905.3502 | one_column | 0.652 | n/a | n/a |
