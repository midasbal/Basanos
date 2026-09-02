# Roadmap

This is the direction, not a set of promises. Almost none of it is built. Everything here runs on the time series the collector is only now starting to gather, so the honest state is that Basanos can record the commons today and will be able to measure it once there is enough record to measure. I am writing the plan down so the direction is public, and so the lines at the bottom sit on record next to the ambitions.

## Where it is now

The collector is built and running: it reads the public commons, captures signed records with enough fidelity to re-verify them, detects and measures the gaps where the ring evicted messages before it could read them, and tracks its own coverage. What does not exist yet is the measurement. Turning the captured record into a published verified-versus-real number is the whole point, and it is the next thing.

## The measurement

These are the questions I want the data to answer, in rough order of how much each depends on the ones before it.

Coverage-weighted uncertainty comes first, because it governs everything else. Every number Basanos publishes should carry a confidence interval and the coverage it was measured at, and no statistic should be shown over a window the collector did not adequately capture. This needs one change in the collector as well, a record of when it was and was not running, so that "coverage" means the fraction of all traffic and not just the fraction of what it saw while awake. A measurement without its own error bars is not one.

Cross-key duplication and content diversity: how much distinct content the commons actually carries against how many distinct keys post it. This is the plainest cut at the verified-versus-real gap, and the one to state most carefully, because some rooms may intend heartbeat-style posting. It is a statement about the shape of the traffic, never about any poster.

Nonce fingerprinting: how a key generates its nonces is a fingerprint of the software behind it, and it is not something a farmer varies by accident. A signal in its own right, and the backbone of the item below it.

Operator clustering: the reframing that matters most. Collapse tens of thousands of keys into the far smaller number of operators actually behind them, by shared templates, nonce families, and co-timing. Every cluster has to be backed by observable evidence, and it stops at the operator, never a person.

Timing and interaction: whether keys post in lockstep, whether the population goes quiet on a human schedule, and whether there is any real reply structure or only parallel monologue. These say less about how many agents there are and more about what kind.

Cohorts over time: group keys by when they first appeared, watch whether each cohort persists or churns, and lay the timeline of airdrop news over the top. This is the one thing that only exists if the collector is recording now, which is why it is recording now.

## Keeping it honest

Two commitments that are as much the project as the measurements are.

Reproducibility. Every published number traces to raw signed records anyone can pull and re-verify, ideally in a single step, including in a browser. The derived dataset and the full methodology get published as a versioned, citable thing, not kept private.

Self-audit. Basanos should be able to recompute the service's own published engagement numbers from raw and say whether they hold, and it should hash-chain and sign its own findings, so the tool meets the standard it holds others to.

## The surface

One view, eventually: the commons as claimed beside the commons as measured, with any figure on screen drilling straight to the signed records beneath it, and a way to re-check the signature yourself. The picture is the point, but only ever on top of numbers that hold up.

## What Basanos will never do

These are not gaps to be filled in later. They are lines.

- It will never score, rank, label, or name an individual identity. Every output stops at the aggregate.
- It will never use an opaque model to decide what is real. Every claim has to be explainable as arithmetic over stated fields and re-derivable by someone else.
- It will never crawl the whole commons exhaustively, and it will never send a single write. It reads, within the service's limits, and nothing more.
- It will never publish a leaderboard or a reputation score, which would only gamify the thing it measures.
- It will never tie a key to anyone or anything outside technocore.chat. That is the line between a measurement and surveillance, and it does not get crossed.