# Accuracy experiments, 6 November 2026

None of the 14 challengers demonstrated a reliable improvement over the current research candidate. None was promoted or deployed. The production forecast and existing prospective comparison protocols are unchanged.

The first registered experiment tested four shot-based Poisson variants, logistic regression and gradient boosting. None improved validation log loss while maintaining the reference's top-outcome accuracy on the same 736 eligible matches from the 2023/24 and 2024/25 seasons. Shots on target are a proxy, not xG.

The second registered experiment tested two Dixon–Coles variants and six probability corrections. Scalar temperature calibration won selection, with log loss 0.960896 versus 0.961343 and identical 55.71% accuracy. It then failed the retrospective audit on 369 eligible 2025/26 matches:

| Metric | Current research candidate | Selected calibration |
| --- | ---: | ---: |
| Correct top outcome | 47.43% | 47.43% |
| Log loss (lower is better) | 1.025579 | 1.026421 |
| Brier score (lower is better) | 0.615846 | 0.616639 |

The day-clustered 95% confidence intervals for both score differences include zero. These periods have been examined in previous research; this audit is retrospective development evidence, not an untouched holdout or a prospective accuracy claim. The selected calibration also failed to improve the 79 common current-season monitoring matches.

## Data findings

The original 4,270-match CSV collection has historical shots, but xG exists for only the 90 current-season rows. One historical away shots-on-target count exceeds the total shots; the parser treats those statistics as missing while retaining the verified match result.

A public WorldfootballR Understat archive was downloaded and decoded: 102,257 shots across 3,996 matches, from August 2014 through 4 January 2025. The raw file's Git blob identity was verified. It has **not** yet been joined to the training data or used to train a challenger. The release channel advertises a larger asset updated in September 2025; its contents remain unverified. The Understat league page currently returns a maintenance page. Historical xG needs fixture/score matching, deduplication and coverage checks before use. Recent coverage also needs to be resolved.

## Reproduction

Install `requirements-experiment.txt`, then run the following against the exact CSVs identified by SHA-256 in the machine-readable report:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.accuracy \
  --data-dir /path/to/cached-csvs --output /tmp/accuracy-first --workers 2

OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.accuracy \
  --data-dir /path/to/cached-csvs --output /tmp/accuracy-calibration --workers 2 \
  --plan docs/research/accuracy-calibration-plan-2026-11-05.json
```

All fitted observations precede the prediction's cutoff, one calendar day before the target match. The classifier refits monthly; Poisson and calibration refit at each prediction cutoff. Selection is saved before the chosen model's retrospective audit. Tests cover future-data exclusion, missing statistics, and recovery of the original model at identity settings. All 38 Python tests pass.

Full settings, per-season metrics, confidence intervals, source hashes and inspection status are in [the JSON report](accuracy-results-2026-11-06.json).
