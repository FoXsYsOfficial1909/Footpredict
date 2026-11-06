# Automatic experiment cycle

The `Automatic bounded experiments` workflow runs after a successful result/xG update, after changes to its registered recipe, or on manual dispatch. It checks out this repository's trusted main branch. Numeric evaluation runs only when eligible model inputs, recipes, implementation or dependency versions change.

The registered procedure compares the fixed `xg100-180` reference with `xg50-180` and `xg100-365`. These recipes and seasons have already been inspected. Repeating them automatically is development monitoring, not a new untouched test. Workload remains excluded while full competition coverage is unverified.

Each run:

1. Loads current verified Football-Data results and the hash-bound last accepted league-summary xG snapshot. Shows missing/stale xG explicitly.
2. Compares semantic eligible inputs and code to the previous successful evaluation. File metadata, unused odds columns and check timestamps cannot force new fitting.
3. Reuses historical predictions only when their exact past inputs, training cutoff, recipe, fixture and implementation match. Corrupt cache entries are recomputed. Past corrections invalidate affected predictions; a target's own score cannot enter its prediction.
4. Evaluates the fixed challengers on the common validation cohort and persists selection before auditing the selected challenger. The reference always receives historical and current-season monitoring, including a confusion matrix and large probability errors.
5. Saves the report, source provenance and cache statistics. An unchanged check preserves the original scientific evaluation timestamp. A failed check retains the previous successful result.

The primary application model and all existing frozen future predictions remain governed by their existing protocols. This workflow never promotes a model or rewrites a forecast.

## Measured reuse

On 4,270 available results and the existing 2,740-match xG snapshot:

| Check | Duration | Fits | Historical cache hits |
| --- | ---: | ---: | ---: |
| Initial complete experiment | 12.75 s | 830 | 0 |
| Identical inputs and restored state | 0.20 s | 0 | Whole result reused |
| Existing prediction cache, fresh procedure state | 6.76 s | 0 | 2,750 |

All scientific results were exactly identical. These are local calculation times, excluding GitHub setup, downloads and publication; they are not the duration of the separate nightly trainer. The initial experiment retained the reference: neither fixed challenger passed the selection rule. Evidence is saved in [automatic-experiments-2026-11-06](automatic-experiments-2026-11-06/benchmark.json).

## Reports and persistence

Readable summaries and 90-day input/result artifacts are available in [GitHub Actions](https://github.com/FoXsYsOfficial1909/Footpredict/actions/workflows/automatic-experiments.yml). The dedicated `experiment-reports` branch contains `latest.json`, immutable `reports/<sha256>.json`, per-run provenance and the last verified successful state. Prediction files and original CSVs use Actions Cache; losing that cache changes speed, not results.

The 24-hour future comparison remains separate. Nine games cannot establish a reliable accuracy improvement. Additional outcomes can update monitoring and motivate new prespecified hypotheses, while historical gains still need prospective confirmation.
