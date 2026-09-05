# Roadmap

This is the direction, not a set of promises. Some of it is built and published; much of it is not. I am writing the plan down so the direction is public, and so the lines at the bottom sit on record next to the ambitions.

## Where it is now

The collector is built and running: it reads the public commons, captures signed records with enough fidelity to re-verify them, detects and measures the gaps where the ring evicted messages before it could read them, and tracks its own coverage and its own uptime. The measurement layer now exists too, and most of it is built and published in FINDINGS.md, each measurement re-verifying every signature from the raw record and reporting a floor with its coverage stated: cross-key duplication, coordination concentration, an activity curve over time, room-controlled timing synchrony, nonce fingerprinting, how many keys post once and vanish and whether they return, how few operators sit behind the shared templates, whether the room is a conversation at all, and a check of the platform's own published numbers against the raw record. What follows lays out the whole direction, marking what is built and what is still ahead.

## The measurement

These are the questions I want the data to answer, in rough order of how much each depends on the ones before it.

Coverage-weighted uncertainty comes first, because it governs everything else. Every number Basanos publishes should carry a confidence interval and the coverage it was measured at, and no statistic should be shown over a window the collector did not adequately capture. (Built: every published number carries the coverage it was measured at and is stated as a floor, and a separate layer adds a confidence interval to each rate that is a genuine sample proportion, kept distinct from the coverage floor rather than conflated with it. At the sample sizes here the sampling interval is negligible, so the coverage floor is the real bound, which the layer states explicitly.) A measurement without its own error bars is not one.

Cross-key duplication and content diversity: how much distinct content the commons actually carries against how many distinct keys post it. This is the plainest cut at the verified-versus-real gap, and the one to state most carefully, because some rooms may intend heartbeat-style posting. It is a statement about the shape of the traffic, never about any poster. (Both built and published: duplication, and a per-key view that measures how many keys post just once and vanish.)

Nonce fingerprinting: how a key generates its nonces is a fingerprint of the software behind it, and it is not something a farmer varies by accident. A signal in its own right, and the backbone of the item below it. (Built and published.)

Operator clustering: the reframing that matters most. Collapse tens of thousands of keys into the far smaller number of operators actually behind them, by shared templates, nonce families, and co-timing. Every cluster has to be backed by observable evidence, and it stops at the operator, never a person. (Built, as a strictly aggregate cluster-size distribution that never names or exposes a key. It currently links keys by shared exact templates only; the nonce-family and co-timing edges named above are still to add, and matter because the single-template linkage both misses the most-shared template and is prone to a transitive-chaining artifact that the size distribution has to be read against. The first real run found many small shared-template clusters and no stable large operator, the apparent large cluster dissolving as the linkage threshold rises.)

Timing and interaction: whether keys post in lockstep, whether the population goes quiet on a human schedule, and whether there is any real reply structure or only parallel monologue. These say less about how many agents there are and more about what kind. (Both built and published. The timing half includes a control that separates real clustering from the room's own rhythm. The interaction half was answered by the data itself: the message format carries no reply field and messages almost never address one another, so there is no interaction structure present to measure.)

Cohorts over time: group keys by when they first appeared, watch whether each cohort persists or churns, and lay the timeline of airdrop news over the top. This is the one thing that only exists if the collector is recording now, which is why it is recording now. (Built and published: keys are grouped by first appearance and tracked into a later window across a gap. It sharpens as the continuous record lengthens, and already does. The airdrop-news overlay is still ahead.)

## Running it, and re-running it

Some of what is left is not new measurement but making the measurements live and checkable over time.

Single-step re-verification. Built: a self-contained static page at docs/verify.html takes a signed record and checks it against its did:key entirely in the browser, with nothing sent anywhere, so a skeptic can confirm a finding without trusting any of this code or installing anything. Its logic is held to the same fixtures the Python verifier is, so the two cannot quietly diverge. This is the concrete form of the reproducibility promise below, and a prerequisite for the surface.

Continuous, scheduled analysis. Every measurement is a manual run today. A scheduled runner that re-runs the measurements over the growing capture and archives timestamped results turns Basanos from a set of snapshots into the longitudinal observatory it is meant to be. The cohort and diurnal measurements in particular sharpen with repetition over time.

Wider-gap cohort re-runs and multi-day patterns. The cohort measurement is parameterized and the diurnal one handles any window length, so these need no new code, only re-running as the continuous capture lengthens. This is already paying off: the single-use finding strengthens as the gap widens, and the activity floor now holds across more than two unbroken days. Whether the curve resolves into a clean daily cycle is still open, since so far the peaks do not line up from one day to the next; more continuous days would settle it.

Coverage beyond the lobby. Everything measured so far is the lobby. The collector also captures the events room, and the commons has others. Extending the measurements across rooms would broaden the findings, though the lobby is where the signal has been, so this is breadth rather than depth.

## Keeping it honest

Two commitments that are as much the project as the measurements are.

Reproducibility. Every published number traces to raw signed records anyone can pull and re-verify, ideally in a single step, including in a browser. The derived dataset and the full methodology get published as a versioned, citable thing, not kept private.

Self-audit. Built: Basanos recomputes the service's own published engagement numbers from the raw record and reports whether they hold, on the snapshots where the exact window can be reconstructed, refusing to compare where it cannot rather than risk a false divergence. And its findings can be notarized: a key-free tool computes a canonical hash of the findings that is signed with the same did:key that posts to the commons, so anyone can confirm, with the same verifier, that the published findings have not been altered since. The tool meets the standard it holds others to.

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