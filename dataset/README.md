# Dataset

This is the derived evidence behind the findings in [../FINDINGS.md](../FINDINGS.md): the actual output of each measurement, plus a small sample of real signed records so the verification chain can be checked end to end.

Everything here is aggregate and derived. It is not a mirror of the commons. The only raw records included are a small, deterministically selected sample, described at the bottom, whose purpose is to let a reader re-verify a signature themselves, not to republish the traffic in bulk.

Each measurement's output carries, inside the JSON, the exact window it was computed over, its coverage, and its re-verify counts, so every file is self-describing. The names below are stable; the original timestamped filename each was produced under is noted so a run can be traced.

## The measurement outputs

These are the exact runs the published findings quote.

- `duplication.json`, cross-key duplication over the reference window. Backs the at-least-45.0-percent cross-key duplication figure. (from duplication_lobby_20260903T210652Z)
- `coordination.json`, coordination concentration and the core-bloc curve over the reference window. Backs the concentration and membership figures, and the disjoint "quiet room" population. (from coordination_lobby_20260903T205143Z)
- `synchrony.json`, timing synchrony with the three-null control (uniform, room, room-minus-self). Backs the finding that the naive timing signal mostly dissolves against the room baseline, with two templates surviving. (from synchrony_lobby_20260903T233839Z)
- `diurnal.json`, the activity curve over the longer continuous span. Backs the finding that the commons does not sleep, sustained across more than two unbroken days. (from diurnal_lobby_20260905T002809Z)
- `nonce.json`, nonce-precision fingerprints per template versus the room. Backs the toolkit-split finding and the "quiet room" nanosecond divergence. (from nonce_lobby_20260904T020721Z)
- `diversity.json`, per-key content diversity and the coverage-stratified single-use rate. Backs the at-least-92.0-percent single-use figure in the best-captured hours. (from diversity_lobby_20260904T184458Z)
- `clustering_min2.json`, operator clustering at a two-shared-template link threshold. Shows the size distribution including the large chained component. (from clustering_lobby_20260904T204024Z)
- `clustering_min3_min4.json`, the same at higher thresholds. Together with the file above, shows the monotone collapse of the largest component that identifies it as a chaining artifact rather than a real operator. (from clustering_lobby_20260904T205402Z)
- `cohort_6h_gap.json`, cohort persistence across a six-hour gap. Backs the 95.1-percent non-return figure. (from cohort_lobby_20260904T193333Z)
- `cohort_16h_gap.json`, cohort persistence across a sixteen-hour gap. Backs the 97.3-percent non-return figure and the progression that shows non-return climbing as the gap widens. (from cohort_lobby_20260905T000416Z)
- `selfaudit.json`, recomputation of the service's own published nick_diversity against the raw record. Backs the finding that the platform figure holds where the window can be fully reconstructed, and that zero_response_share is structurally unauditable. (from selfaudit_lobby_20260904T215634Z)
- `tclk.json`, the tclk deal lifecycle from the signed transcript: frame counts by type and the offer-to-completion funnel. Backs the finding that offers vastly outnumber any downstream progression (30,884 offers, 7 accepted, 6 completed), stated as a floor with the protocol's alpha status explicit. (from tclk_lobby_20260906T071754Z)
- `uncertainty_cohort.json`, Wilson intervals over the cohort output. (from uncertainty_cohort_lobby_20260904T193333Z)
- `uncertainty_diversity.json`, Wilson intervals over the diversity output, showing the single-use rate's sampling interval is negligible. (from uncertainty_diversity_lobby_20260904T184458Z)
- `uncertainty_duplication.json`, Wilson intervals over the cross-key duplication rate, showing the headline figure's sampling interval is negligible at this sample size. (produced by analysis.uncertainty over duplication.json)
- `uncertainty_coordination.json`, Wilson intervals over the coordinated share and top-N concentration, both negligible at this sample size. (produced by analysis.uncertainty over coordination.json)
- `uncertainty_tclk.json`, Wilson intervals over the tclk completion and acceptance rates; the completion interval spans nearly fivefold, the quantitative form of the small-numerator caveat the finding states in prose. (produced by analysis.uncertainty over tclk.json)

## The sample of signed records

`sample_signed_records.jsonl` is 100 signed message records, selected by a rule anyone can reproduce: the first 100 records containing a signature field, in file order, from the lobby capture used for the headline measurements. No record was hand-picked.

Its only purpose is to let a reader confirm the verification chain without capturing their own data: take any record from this file, open [../docs/verify.html](../docs/verify.html) in a browser, paste the record in, and the page decodes the did:key and checks the Ed25519 signature entirely in the browser, with nothing sent anywhere. A valid record verifies; altering any character of its text or signature makes it fail.

This is a sample, not the dataset. Basanos does not publish a bulk archive of the commons; it publishes what it measured, and enough raw material to check that the measuring is honest.

## Reproducing any figure

Every number in the findings traces to one of the outputs above, and each output was produced by re-verifying every signature from the raw record and aggregating, never by trusting a stored signature. The code that produces these files is in [../analysis/](../analysis/), and the verification logic every one of them depends on is in [../collector/verify.py](../collector/verify.py), mirrored in the browser page above.