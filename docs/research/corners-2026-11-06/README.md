# Corners: first release and prospective test

The application now has a separate total-corner estimate inside the selected match. The public estimate is explicitly the empirical EPL reference, not a personalized team forecast. It shows the league's expected total and probabilities of at least 9, 10 and 11 corners. The normal three-tab interface and the existing 1X2 forecast are unchanged. Weather, injuries and workload are not added to corner probabilities.

All 4,270 source fixtures have valid paired HC/AC counts. Three team-strength recipes were registered before evaluation: Poisson with 365-day decay, Poisson with 180-day decay, and a negative-binomial total with 180-day decay. Selection used 760 complete fixtures from seasons 2023/24 and 2024/25; the selected Poisson-365 recipe was then evaluated on the 380 fixtures of 2025/26.

| Retrospective audit, 380 fixtures | Team model | Empirical league reference |
| --- | ---: | ---: |
| Mean binary Brier across three thresholds | 0.246475 | 0.240099 |
| Mean binary log loss | 0.690644 | 0.673203 |
| Mean absolute total-count error | 2.682538 | 2.645861 |

The selected team model failed the registered audit gate. Current-season monitoring of 90 fixtures favors it on probability scores, but that smaller sample does not overturn the failed audit or prove a stable advantage. The public-serving policy therefore remains `empirical_league`. Home and away league averages are not presented as personalized team means.

`research.corner_nightly` refreshes the empirical estimate daily at 00:43 UTC using only historical matches strictly before the preceding calendar day. Exact canonical model bytes are published by hash on the separate `corner-reports` branch. The browser checks their SHA-256, version, target, parameters and freshness before rendering. Missing, stale or invalid data produce an unavailable message; zero corners remain valid outcomes.

## Independent future comparison

The selected team model and empirical reference were frozen together at **2026-11-06 17:46:48 UTC** for five eligible EPL fixtures on **8 November**, with at least 24 hours lead. Saturday fixtures did not meet that lead-time rule. This is separate from the existing nine-match xG packet, which was not modified.

The immutable packet SHA-256 is `36da27d3c59791b7e79af3fc13868226a1f8a517a175f406a73d7b90a8f9a2b5`. Independent JavaScript inference reproduced all 15 team-model threshold probabilities with maximum absolute difference 4.45e-16. At registration there are five pending results and no measured future corner accuracy.

The `Frozen corner prospective comparison` workflow checks outcomes every six hours. Fixture identity and goal-score agreement must be verified before Football-Data HC/AC counts can settle the pair. Missing counts remain pending; changed dates/teams or score conflicts exclude the pair. Both distributions use the same settled cohort. Three correlated thresholds are not three independent matches, and corner-event accuracy is not 1X2 accuracy.

Full selection, audit, frozen inputs, model and per-match future probabilities are archived in this directory. No superiority or calibrated probability claim is made for the team model.
