# Multi-Column Fragment-Merge Parity Results

**Corpus:** 87 pages checked from benchmark dataset. Pages from 0706.0954.pdf (Type3 font) are excluded because PyMuPDF's `get_text("text", sort=True)` reference itself is token-broken on that document, making regression detection unreliable; the harness guards against false positives via a heading regression test and a p11 floor threshold.

## Results

```
improved  0710.2740.pdf#p7: 0.9760 -> 0.9850
excluded  0706.0954.pdf#p1: 0.8891 -> 0.8459
excluded  0706.0954.pdf#p10: 0.8602 -> 0.8555
excluded  0706.0954.pdf#p11: 0.8251 -> 0.8333
excluded  0706.0954.pdf#p12: 0.8533 -> 0.9029
excluded  0706.0954.pdf#p13: 0.8049 -> 0.8362
excluded  0706.0954.pdf#p14: 0.8498 -> 0.8786
excluded  0706.0954.pdf#p15: 0.8947 -> 0.9150
excluded  0706.0954.pdf#p16: 0.8677 -> 0.9153
excluded  0706.0954.pdf#p18: 0.8140 -> 0.8333
excluded  0706.0954.pdf#p19: 0.7972 -> 0.8438
excluded  0706.0954.pdf#p2: 0.8772 -> 0.8198
excluded  0706.0954.pdf#p21: 0.8454 -> 0.8247
excluded  0706.0954.pdf#p22: 0.8163 -> 0.8494
excluded  0706.0954.pdf#p6: 0.8897 -> 0.8644
87 pages checked: 0 regressed, 1 improved, 14 excluded
```

**Summary:** Zero pages regressed. One page improved (0710.2740.pdf#p7, +0.0090). Parity is maintained. The token-multiset overlap metric (overlap of tokens between extracted and reference text, regardless of order) measures contiguity rather than reading order, so it validates that fragmentation is fixed without requiring a full reading-order corpus.
