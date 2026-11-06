# xG model research, 6 November 2026

The new league-summary xG challenger improves retrospective metrics over the current research candidate. It has not replaced the application's production forecast. The improvement is small and confidence intervals include zero, so a new prospective comparison is required.

| Common cohort | Current research candidate accuracy | xG challenger accuracy | Reference log loss | xG log loss |
| --- | ---: | ---: | ---: | ---: |
| Selection, 2023/24 and 2024/25 (736) | 55.71% (410) | 56.25% (414) | 0.961343 | 0.955917 |
| Retrospective audit, 2025/26 (369) | 47.43% (175) | 49.32% (182) | 1.025579 | 1.023550 |
| Current-season monitoring (79) | 45.57% (36) | 46.84% (37) | 1.059055 | 1.034996 |

Audit Brier improves from 0.615846 to 0.614481; current-season Brier improves from 0.636371 to 0.622938. The audit's paired day-clustered 95% intervals are [-0.010318, 0.007247] for the Brier difference and [-0.014699, 0.009952] for log loss. Both include zero. Current-season intervals also include zero. These periods have been inspected previously; they are retrospective development evidence. The 79-match monitoring result amounts to one additional correct outcome and must not be advertised as a reliable percentage-point gain.

## What was trained

Four recipes were registered before each evaluation. Selection chose `xg100-180`: team attack/defence Poisson quasi-likelihood, 180-day decay half-life, ridge 0.03, three-year window, with 75% Poisson probabilities and 25% outcome-based Elo. The Poisson training target uses historical match xG scaled by an exclusively past weighted goals/xG ratio. Matches missing xG retain their actual-goal targets. Actual result labels never change.

Only information dated strictly before the day preceding each target match enters its prediction. Target-match xG is never a feature. Every selection and its plan, source data, implementation and original reference recipe are hashed before the chosen challenger is audited. Complete seasons and common cohort sizes are checked. The current reference uses the fixed settings in `selection.json`, rather than the original production v1 or a silently changed comparison series.

## Data and source consistency

The final snapshot contains **2,740 league-summary xG observations**: every match from the 2019/20 through 2025/26 seasons, plus 80 of the first 90 current-season matches through 25 October 2026. All match dates, canonical teams and final scores match the original Football-Data results exactly.

Raw league-summary CSVs are attributed to [elliot-gautier-129/footy-prediction](https://github.com/elliot-gautier-129/footy-prediction/tree/90233c5edc2cd4a07011ea642b27ea479d106bf5/data/raw/understat/eng), supplemented by raw xG facts in [CameronNichol47/epl-match-prediction-dashboard](https://github.com/CameronNichol47/epl-match-prediction-dashboard/tree/647420c6bb756db992374b04a5ba0ee6235a2624/data/processed). All 28 CSV blobs were checked against pinned Git SHA-1 and SHA-256 identities. **All 414 overlapping match xG pairs agree exactly.** No external application features, forecasts or model code are imported.

The original WorldfootballR shot archive was separately validated, aggregated and tested in the first experiment. Its raw shot-xG sums differed from league-summary xG on 23 of 38 overlapping fixtures. A scratch rebound-chain calculation explained all 38 within 0.0001, but the final model uses direct league-summary values instead of an inferred aggregation formula. The raw-shot experiment improved the older audit but worsened current monitoring; it is retained separately for review.

The last ten completed games have no xG in this snapshot and use actual goals during training. Understat's public site and league-data endpoint currently return a maintenance page. These pinned public caches provide reproducible research data; they are not a guaranteed live feed. No continuous xG refresh is claimed.

## Reproduce

Install `requirements-research.txt`; `requirements-xg.txt` is needed only to decode the optional original RDS shot archive. Supply the exact original Football-Data CSVs identified in the source manifest, then run:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.xg_experiment \
  --data-dir /path/to/original-csvs \
  --xg-matches docs/research/xg-2026-11-06/xg-matches.json \
  --source-manifest docs/research/xg-2026-11-06/xg-manifest.json \
  --plan docs/research/xg-summary-plan-2026-11-06.json \
  --output /tmp/footpredict-xg-summary
```

`research.xg_summary` and `research.xg_recent` can download the exact pinned raw files and rebuild the normalized data. Their output was verified byte-for-byte against the snapshot. Its SHA-256 is `4fc439e87c145c44bd0d970c8ccb2339be91b0a64d360285c3716097d0254153`.

Full metrics, per-match predictions, source manifests and both experimental records are in [xg-2026-11-06](xg-2026-11-06/summary-summary.json).

## New prospective comparison

The independent `xg-paired-1` packet was frozen at **2026-11-06 03:48:36 UTC** for the first nine eligible EPL fixtures on 7–8 November. Both exported models and both predictions were saved together, at least 24 hours before every kickoff. The first is Leeds–Tottenham at 12:30 UTC on 7 November, more than 32 hours after registration. Everton–Coventry on 6 November is excluded because it no longer meets the 24-hour rule. No existing application comparison series or prediction was replaced.

Both models were fitted once on the same 4,270 verified match results, excluding dates on or after 5 November. Final Poisson windows contain 1,121 matches; the xG model has 1,111 paired xG observations (95.86% of its decay-weighted window). All 90 current-season Football-Data CSV results were independently checked against the current football-data.org response without conflicts. Frozen xG coverage ends on 25 October; the ten missing games retain actual-goal targets.

The [forecast packet](xg-2026-11-06/prospective/packet.json) includes both model exports, all nine fixture identities, probabilities, source and training hashes. Its canonical SHA-256 is `9a66a0c946cf34175a9159276e266c15f7eec804a6cadfa729b565838552b3f7`. Compressed training inputs are archived beside it. A cold refit reproduced the complete packet exactly; independent JavaScript inference verified all 18 probability vectors with maximum absolute difference 4.45e-16.

At registration: **9 pending, 0 settled, 0 excluded**; future accuracy is unavailable. The `Frozen xG prospective comparison` GitHub Actions workflow checks outcomes every six hours, without refitting or altering forecasts. A changed date or team, cancellation or postponement excludes the entire pair; only confirmed final scores count for both models. Every successful run saves its report and provider response as a 90-day artifact and a workflow summary. Failed retrievals fail visibly and do not create zero-accuracy reports. This workflow does not refresh the xG dataset or promote a model.
