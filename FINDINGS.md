# Findings

This records what Basanos has measured so far. It covers the first two measurements the analysis layer produces, run against the live lobby on technocore.chat. Both are aggregate, structural statements about the traffic. Neither scores, ranks, labels, or names any identity, and neither ever will.

Everything here is a floor, stated next to the coverage it was measured at. "At least this much" is the only claim the numbers make. Every figure below was recomputed from the captured records by re-verifying each signature from scratch, never by trusting a signature the way it was stored.

## How to read these numbers

Two things bound every figure, and both push the same way.

Coverage is a floor. The collector captures the lobby only as fast as it can read against a service that sheds load with HTTP 503s and evicts messages from a fixed-size ring before they can be read. It records exactly how much it lost, so coverage is a measured fraction, not a guess. In the windows below, coverage sits between roughly 74 and 88 percent. Anything measured at less than full coverage is a floor twice over, because the stretches the collector missed are the burstiest ones, and duplicated traffic concentrates in bursts. If anything, the parts never seen are more duplicate-heavy than the parts seen.

Exact match is a floor too. Both measurements match text byte for byte. A single altered character, a stray space, one different emoji encoding, splits what is otherwise the same line into two. So every count of shared or duplicated text is a lower bound on the true amount of reuse. A concrete instance of this shows up in the data and is described under Caveats below.

A window is a stretch of captured traffic frozen at a point in time. Two windows are reported here, an earlier smaller one and a later larger one, and the later contains the earlier. They are not a time series, and the difference between their numbers should not be read as change over time. The reasons are under Caveats.

## Measurement 1: cross-key duplication

The question: among signed messages that re-verify, what share carry text that is byte-identical to text signed by at least one other distinct key in the same window?

This is the plainest cut at the gap between verified and real. A key holding its own signing key and posting a line that thousands of other keys also post, verbatim, is a verified participant by the only test the commons applies. Whether it is a real one is the open question. The measurement does not answer that. It measures how much of the verified traffic carries non-distinct content across distinct keys, and reports it as a floor, with the caveat that some rooms may intend heartbeat-style posting. It is a statement about the shape of the traffic, never a verdict about any poster.

Window A, 560,480 re-verified signed lobby messages, coverage 74.4 percent: at least 25.8 percent are cross-key duplicates, 144,694 of 560,480. Across those messages there were 386,731 distinct signing keys and 407,926 distinct texts, close to one distinct line per key.

Window B, 2,463,428 re-verified signed lobby messages, coverage 88.3 percent: at least 45.0 percent are cross-key duplicates, 1,108,558 of 2,463,428. 1,140,981 distinct keys, 1,390,344 distinct texts.

The two windows disagree by a lot, and that disagreement is itself a finding: the measured rate is not stable across the two windows captured. What is stable is the presence and scale of the phenomenon. In both, hundreds of thousands of verified keys post lines that many other verified keys post verbatim, and the top templates are each signed by thousands of distinct keys. In window B the single most-shared line was signed by 14,294 distinct keys.

## Measurement 2: coordination concentration

Duplication says how much text is shared across keys. This asks how concentrated that sharing is: how few templates carry it, and how many keys draw from the same small set.

Three cuts, all aggregate, all over the same re-verified population.

Concentration. In window B, the 20 most-shared templates, ranked by how many distinct keys signed each, account for 39.6 percent of all cross-key-duplicated messages. In window A, the top 20 account for 65.6 percent. A small set of lines carries a large share of the duplication.

Shared-template membership per key. Counting, for each key, how many of the top 20 templates it signed:

In window B, at least 40,122 keys signed at least one of the top 20, at least 16,842 signed at least five, at least 10,563 signed at least ten, and at least 1,848 signed at least fifteen. No key signed all twenty, and the set of keys that signed every one of the top 20 is empty.

In window A the same curve is far shallower: at least 20,910 keys signed at least one, 6,463 signed at least five, only 186 signed at least ten, and none signed fifteen or more.

What this supports, and what it does not. Byte-identical text shared across thousands of distinct keys is strong evidence of shared origin: a common template, a shared toolkit, a single operator behind many keys. That is coordination linkage, and it is what these numbers measure. It is not a census of operators. Shared library text could link genuinely independent authors, and the empty full-intersection means there is no single bloc signing everything. The honest reading is that many verified keys draw from a shared pool of lines, not that a known number of operators stands behind them. How few operators actually stand behind these keys remains open.

One structural detail holds up cleanly in window B and is worth stating because it complicates any single story. Among the top five templates, the largest, signed by 14,294 keys, shares almost no keys with the other four: its pairwise overlap with each of them is essentially zero. The other four overlap heavily with each other, sharing well over half their key sets pairwise. So the duplication is not one undifferentiated mass. There are at least two distinct populations in it, one cycling a set of mutually-overlapping lines and another posting a single line and little else.

## Measurement 3: activity over time, and whether the duplication is timed

The first two measurements are about content: the same text signed by many keys. This one is about time: when those posts arrive. It has two parts, an activity curve and a timing test, and the second depends on the first.

All figures here are from window B, the 2,463,428 re-verified signed messages spanning about 32 hours, at 88.2 percent coverage.

### The commons does not sleep

Binning re-verified signed posts by their server timestamp into one-hour bins across the window gives the lobby's activity curve. Because the collector loses more to ring eviction when traffic is heaviest, the raw captured count understates the busy hours, so the honest curve is captured posts plus the collector's own recorded eviction count, its best estimate of true throughput, with per-hour coverage stated between the two.

Across the whole window the estimated throughput never falls below roughly 65,000 posts an hour. There is no nightly collapse toward quiet, no trough that a population living on human waking hours would produce. The busiest hours run to about 160,000, so the curve swings by roughly two and a half times, but its floor stays high around the clock. The swing is also not a smooth daily rise and fall: it has sharp peaks, one in the evening of the first day and a cluster across the second morning, more consistent with automated activity responding to something than with people waking and sleeping.

This is a single window of about a day, so it is an activity curve, not yet a confirmed daily cycle: distinguishing a repeating pattern from one quiet stretch needs several continuous days, which continued collection will provide. But within this window, the shape points away from a human rhythm.

### Whether the duplicated templates are timed together, or just ride the room

If thousands of keys post the same line, do those posts cluster in time, which would suggest a shared scheduler, or spread out the way independent posting would? The measure is the index of dispersion of each template's posts across ten-second buckets: near one is spread like random, well above one is clustered, below one is more even than random.

The trap is that the room itself is not flat, it swings two and a half times, so a template that simply posts more when the room is busy will look clustered even with no coordination of its own. To separate the two, each template's clustering is compared against three baselines: a naive uniform one, one weighted by the room's own activity, and one weighted by all other traffic with the template's own posts removed, so it is never measured against a baseline built partly from itself. The last is the honest one.

Against the naive baseline the fifteen most-duplicated templates look clustered, at roughly twice random. Against the room's own rhythm that mostly dissolves: their clustering drops to about 1.2 times, meaning most of the apparent timing structure was simply these templates riding the room's activity swing, not coordinating with each other. Some slight excess clustering remains, but it is modest, and the text duplication those templates show is not matched by tight timing.

Two smaller templates are the exception. They stay clustered at three to four times even against the all-other-traffic baseline, so their posts really do bunch in time beyond anything the room's rhythm explains. These are the same two that stood apart structurally in the second measurement, which is a small independent corroboration that they behave differently from the rest.

So on the timing axis the coordination story is weaker than the content axis: most of the heavily-duplicated traffic posts roughly when the room posts, with a specific pair of templates as the exception. The strong, unambiguous finding across all three measurements is the content one, that thousands of verified keys post byte-identical text. Whether that text is also released on a shared clock is, for most of it, not supported here.

## Measurement 4: nonce fingerprints, an independent axis

The first three measurements all rest, one way or another, on message text: who posts the same words, how concentrated that is, and when those words arrive. This one deliberately ignores text and looks at something a poster does not choose for its meaning: the nonce it signs.

Every signed message carries a nonce, and in this commons the nonce is a Unix timestamp. What varies between posters is the precision of that timestamp, visible directly in how many digits it has: 13 digits is a millisecond clock, 16 is microsecond, 19 is nanosecond. That precision is a fingerprint of the software behind the key, not of anything the poster wrote. Two keys posting identical text could run different code and show different nonce precisions; two keys posting unrelated text could run the same code and show the same one. So nonce precision is an axis independent of the text, and where it lines up with the text findings, that agreement is worth more than either signal alone.

All figures here are from window B, the same 2,463,428 re-verified signed messages at 88.2 percent coverage. Every signed message had a usable numeric nonce.

Across the whole lobby, the nonce precisions split three ways: about 78 percent millisecond, 14 percent nanosecond, 8 percent microsecond, with a small remainder at other lengths. So the room is dominated by one clock precision but plainly contains more than one.

The revealing part is per template. Nineteen of the twenty most-duplicated templates are essentially pure millisecond: their keys, thousands each, almost all sign millisecond timestamps, and they all differ from the room-wide mix by the same small amount, the amount you would expect from being purely one precision against a room that is mostly but not entirely that precision. On this axis those nineteen are homogeneous and unremarkable.

One template is not. The single largest, posted by more than fourteen thousand keys, is essentially pure nanosecond, standing far apart from every other template and from the room. Its keys run a different clock. And this is the same template that stood alone in the coordination measurement, the one whose set of signing keys barely overlapped any other template's. Two measurements that share no inputs, one built on which keys post the same text and one built on the digit length of a timestamp, both single out the same population as separate from the rest. That convergence is the strongest evidence in this document that this particular population is a distinct operation, precisely because the two signals could so easily have disagreed and did not.

It is worth being equally clear about what this measurement did not find. The two templates that showed genuine timing clustering in the previous section are, on the nonce axis, ordinary: pure millisecond, indistinguishable from the main cluster. So whatever makes them cluster in time, it is not a distinct nonce toolkit. The nonce axis corroborates one population and declines to corroborate another, which is the behavior of a measurement that reports what is there rather than what would be convenient. That it can decline is the reason its agreement, where it does agree, is worth trusting.

## What is not claimed

The persistence question is open. Whether the gap between verified and real is growing, shrinking, or steady over time is the question Basanos exists to answer, and it is not answered here. The two windows are not comparable as a time series, so nothing about the direction of change can be read from them. Answering it needs windows captured continuously across a known span, which is what the next measurements and continued collection are for. Timing and diurnal structure are the first cut at it.

No count here is an operator count. The key totals are counts of distinct signing keys, nothing more. Collapsing keys toward the operators behind them is a later, evidence-backed measurement, and even then it stops at the operator and never reaches a person.

## Caveats and known issues

These are stated because the numbers are not trustworthy without them.

The two windows are not a time series, and they are not even independent. The collector never truncates its message log, so the two are nested: window A is an earlier, smaller freeze of the same continuously growing file that window B was frozen from later. Window A holds messages up to its freeze on 2026-09-02; window B holds the same messages plus everything captured through the next day, spanning about 32 hours of server time. That span also contains a collector restart, at which the coverage counters reset, so window B is a long window with an internal discontinuity rather than a clean single run. Because window A is a prefix of window B, the gap between their duplication rates, 25.8 versus 45.0 percent, cannot be read as change over time: it is a comparison between a window and a superset of itself, under different coverage, not two points on a line. What it does show is that the measured rate depends heavily on which stretch of traffic is in view. Establishing a real trend needs windows captured continuously across known, non-overlapping spans, which is what continued collection and the next measurements are for.

An encoding split undercounts one template. In window B the same line appears as two separate templates, one with a correctly encoded emoji signed by 12,148 keys and one with the same emoji mangled by a byte-encoding error signed by another 1,114 keys. They are the same line. Because both measurements match exactly, they are counted separately, which means the true reuse of that line is higher than either number alone and the overall floors are, for this reason too, undercounts. The mangling is a defect in the capture-and-store path, and correcting it, along with a tolerance for exactly this kind of trivial variant, is what the planned near-duplicate measurement is for.

One signature failed to re-verify. Across the 2,463,428 signed messages in window B, exactly one did not re-verify and was excluded from every count. It is reported rather than dropped silently, because re-verifying every signature from the stored record, instead of trusting how it was stored, is the point. The pipeline caught it.

## Reproducing these

Every number above was produced by streaming the captured lobby record, re-verifying each Ed25519 signature against the signed message, and aggregating. The two analysis modules that produce these figures independently arrive at the same duplication rate, distinct-key count, distinct-text count, and re-verify-failure count on the same captured window, which is the internal cross-check that the shared definition is implemented the same way in both. The captured records carry every signed field verbatim, so any figure here traces back to signed messages that can be pulled and checked. A single-step, in-browser re-verification path is planned and not yet built.