# Workload collection status

The public ESPN calendars contain 15,453 completed events across fourteen registered club competitions from January 2019 through 5 November 2026. Every one of the 2,930 overlapping EPL results matches Football-Data dates, teams and scores. Compressed observations and source-response hashes are preserved here. No numerical workload correction has been trained or enabled.

The complete source retrieval does not prove complete coverage of every competition. Four FA Cup/League Cup count checks agree with Openfootball; another league season has missing postponed fixtures. July-based calendar season labels also split pandemic and summer Club World Cup seasons, so matching raw seasonal counts must not be used as a completeness shortcut. The manifest remains explicitly partial. Unknown rest and extra time remain unknown.

`research.workload_data` can fetch registered ESPN scoreboards and rebuild normalized observations with hash, duplicate, date, score and response-limit checks. `research.workload` computes numeric rest only for independently verified complete scopes; otherwise it exposes observed lower bounds. `research.workload_model` and `research.workload_experiment` implement the registered bounded correction experiment, ready for sufficiently verified data.

The upcoming calendar context is a separate observation collected after the original xG forecast packet. It is not an input to, or replacement for, those nine frozen forecasts. Arsenal and Manchester City have recorded Champions League games on 4 November before their 7 November fixtures; several other clubs have European games on 5 November. These observations alone do not certify true total rest.

To inspect the exact normalized JSON, decompress `workload-events.json.gz`. Its uncompressed SHA-256 is recorded in `status.json` and the source manifest. Downloaded original ESPN responses are not bundled; their registered URLs and SHA-256 identities are retained in the manifest.
