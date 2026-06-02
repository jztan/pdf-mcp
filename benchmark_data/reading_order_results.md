# Reading-order fidelity benchmark

Score = sequence similarity of normalized token streams vs READoc ground truth (higher is better, max 1.0). `pdfmcp` = current `extract_text_from_page`; `p4llm_ref` = PyMuPDF4LLM column-aware reference (upper bound).

## Aggregates

| group | n | pdfmcp | p4llm_ref | delta |
| --- | --- | --- | --- | --- |
| two_column | 22 | 0.815 | 0.860 | +0.045 |
| one_column | 22 | 0.836 | 0.860 | +0.025 |

## Per-document

| id | group | pdfmcp | p4llm_ref |
| --- | --- | --- | --- |
| 0707.1301 | two_column | 0.762 | 0.770 |
| 0709.4466 | two_column | 0.930 | 0.921 |
| 1207.2761 | two_column | 0.869 | 0.901 |
| 1301.3570 | two_column | 0.699 | 0.760 |
| 1302.3440 | two_column | 0.839 | 0.887 |
| 1302.4245 | two_column | 0.829 | 0.921 |
| 1307.7059 | two_column | 0.952 | 0.989 |
| 1401.4991 | two_column | 0.797 | 0.865 |
| 1406.4582 | two_column | 0.700 | 0.845 |
| 1406.6799 | two_column | 0.908 | 0.865 |
| 1409.7193 | two_column | 0.649 | 0.757 |
| 1501.05624 | two_column | 0.895 | 0.963 |
| 1601.06071 | two_column | 0.790 | 0.900 |
| 1606.06090 | two_column | 0.915 | 0.961 |
| 1612.09007 | two_column | 0.805 | 0.890 |
| 1712.00712 | two_column | 0.753 | 0.780 |
| 1807.03386 | two_column | 0.862 | 0.905 |
| 1807.11632 | two_column | 0.861 | 0.899 |
| 1808.03354 | two_column | 0.953 | 0.969 |
| 1808.08321 | two_column | 0.779 | 0.783 |
| 1811.03679 | two_column | 0.473 | 0.477 |
| 1910.03474 | two_column | 0.909 | 0.920 |
| 0705.4297 | one_column | 0.921 | 0.953 |
| 0706.0028 | one_column | 0.899 | 0.931 |
| 0706.0954 | one_column | 0.457 | 0.596 |
| 0706.2397 | one_column | 0.843 | 0.772 |
| 0707.0311 | one_column | 0.920 | 0.927 |
| 0707.3690 | one_column | 0.693 | 0.803 |
| 0707.4042 | one_column | 0.940 | 0.919 |
| 0709.2178 | one_column | 0.914 | 0.936 |
| 0709.2857 | one_column | 0.779 | 0.782 |
| 0710.2265 | one_column | 0.907 | 0.957 |
| 0710.2740 | one_column | 0.941 | 0.961 |
| 0711.0528 | one_column | 0.915 | 0.970 |
| 0711.3236 | one_column | 0.901 | 0.997 |
| 0802.0539 | one_column | 0.785 | 0.817 |
| 0802.0733 | one_column | 0.911 | 0.979 |
| 0811.0781 | one_column | 0.875 | 0.900 |
| 0811.0851 | one_column | 0.911 | 0.545 |
| 0902.1533 | one_column | 0.899 | 0.943 |
| 0903.1810 | one_column | 0.921 | 0.979 |
| 0904.1520 | one_column | 0.857 | 0.882 |
| 0905.2570 | one_column | 0.695 | 0.735 |
| 0905.3502 | one_column | 0.499 | 0.643 |
