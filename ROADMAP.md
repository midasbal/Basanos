# Roadmap

This is the direction, not a set of promises. Some of it is built and published; much of it is not. I am writing the plan down so the direction is public, and so the lines at the bottom sit on record next to the ambitions.

## Where it is now

The collector is built and running: it reads the public commons, captures signed records with enough fidelity to re-verify them, detects and measures the gaps where the ring evicted messages before it could read them, and tracks its own coverage and its own uptime. The measurement layer now exists too. Several measurements are built and their results published in FINDINGS.md, each re-verifying every signature from the raw record and reporting a floor with its coverage stated: cross-key duplication, coordination concentration, an activity curve over time, room-controlled timing synchrony, and nonce fingerprinting. What follows is the rest of the direction, some of it built, some still ahead.

## The measurement

These are the questions I want the data to answer, in rough order of how much each depends on the ones before it.

Coverage-weighted uncertainty comes first, because it governs everything else. Every number Basanos publishes should carry a confidence interval and the coverage it was measured at, and no statistic should be shown over a window the collector did not adequately capture. (Partly built: every published number already carries the coverage it was measured at and is stated as a floor, and the collector now records when it was and was not running. The formal confidence interval on each statistic is still to come.) A measurement without its own error bars is not one.

Cross-key duplication and content diversity: how much distinct content the commons actually carries against how many distinct keys post it. This is the plainest cut at the verified-versus-real gap, and the one to state most carefully, because some rooms may intend heartbeat-style posting. It is a statement about the shape of the traffic, never about any poster. (Duplication is built and published; the content-diversity view is still ahead.)

Nonce fingerprinting: how a key generates its nonces is a fingerprint of the software behind it, and it is not something a farmer varies by accident. A signal in its own right, and the backbone of the item below it. (Built and published.)

Operator clustering: the reframing that matters most. Collapse tens of thousands of keys into the far smaller number of operators actually behind them, by shared templates, nonce families, and co-timing. Every cluster has to be backed by observable evidence, and it stops at the operator, never a person. (Built, as a strictly aggregate cluster-size distribution that never names or exposes a key. It currently links keys by shared exact templates only; the nonce-family and co-timing edges named above are still to add, and matter because the single-template linkage both misses the most-shared template and is prone to a transitive-chaining artifact that the size distribution has to be read against. The first real run found many small shared-template clusters and no stable large operator, the apparent large cluster dissolving as the linkage threshold rises.)

Timing and interaction: whether keys post in lockstep, whether the population goes quiet on a human schedule, and whether there is any real reply structure or only parallel monologue. These say less about how many agents there are and more about what kind. (The timing half is built and published, including a control that separates real clustering from the room's own rhythm. The interaction and reply-structure half is still ahead.)

Cohorts over time: group keys by when they first appeared, watch whether each cohort persists or churns, and lay the timeline of airdrop news over the top. This is the one thing that only exists if the collector is recording now, which is why it is recording now. (Not built. Along with any multi-day pattern, this is gated on the collector accumulating continuous days, which it is now doing.)

## Running it, and re-running it

Some of what is left is not new measurement but making the measurements live and checkable over time.

Single-step re-verification. Every published number traces to signed records, but re-checking one today means cloning the repo and running Python. A small self-contained verifier, and eventually a static in-browser page, that takes a signed record and checks it against its did:key in one step, is what lets a skeptic confirm a finding without trusting any of this code. This is the concrete form of the reproducibility promise below, and a prerequisite for the surface.

Continuous, scheduled analysis. Every measurement is a manual run today. A scheduled runner that re-runs the measurements over the growing capture and archives timestamped results turns Basanos from a set of snapshots into the longitudinal observatory it is meant to be. The cohort and diurnal measurements in particular sharpen with repetition over time.

Wider-gap cohort re-runs and multi-day patterns. The cohort measurement is already parameterized, and the diurnal one already handles any window length, so these need no new code, only re-running as the continuous capture lengthens. The single-use-across-time finding gets stronger with a wider gap, and the activity curve becomes a confirmed daily cycle rather than a single window, on their own, with patience.

Coverage beyond the lobby. Everything measured so far is the lobby. The collector also captures the events room, and the commons has others. Extending the measurements across rooms would broaden the findings, though the lobby is where the signal has been, so this is breadth rather than depth.

## Keeping it honest

Two commitments that are as much the project as the measurements are.

Reproducibility. Every published number traces to raw signed records anyone can pull and re-verify, ideally in a single step, including in a browser. The derived dataset and the full methodology get published as a versioned, citable thing, not kept private.

Self-audit. Basanos should be able to recompute the service's own published engagement numbers from raw and say whether they hold, and it should hash-chain and sign its own findings, so the tool meets the standard it holds others to. The collector already captures the service's own engagement figures alongside every commons snapshot, so this is a matter of defining the comparison, not of gathering new data.

Hardening the checks themselves. The verification path parses fully attacker-controlled input, so it is fuzzed against arbitrary bytes to confirm only expected errors ever escape; the test suite runs in continuous integration, not only when someone remembers; and the single-step verifier above is held to the same fixtures the Python path is, so the two can never quietly diverge. The tool's own rigor has to be checkable, not asserted.

## The surface

One view, eventually: the commons as claimed beside the commons as measured, with any figure on screen drilling straight to the signed records beneath it, and a way to re-check the signature yourself. The picture is the point, but only ever on top of numbers that hold up.

## What Basanos will never do

These are not gaps to be filled in later. They are lines.

- It will never score, rank, label, or name an individual identity. Every output stops at the aggregate.
- It will never use an opaque model to decide what is real. Every claim has to be explainable as arithmetic over stated fields and re-derivable by someone else.
- It will never crawl the whole commons exhaustively, and it will never send a single write. It reads, within the service's limits, and nothing more.
- It will never publish a leaderboard or a reputation score, which would only gamify the thing it measures.
- It will never tie a key to anyone or anything outside technocore.chat. That is the line between a measurement and surveillance, and it does not get crossed.