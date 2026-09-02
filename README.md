# Basanos

Basanos measures the gap between verified and real participation in the Technocore agent commons. A basanos is the stone an assayer uses to tell real gold from fake by the mark it leaves.

## Why

A new kind of economy is being built on a single premise: that if an agent holds a cryptographic key and signs with it, that signature is enough to count it as a real participant, one worth compute, worth payment, worth a place in the commons. Flop Labs, led by Arthur Hayes, is building toward that idea, with $FLOP as the currency agents spend and Technocore as the place they gather and sign.

A signature proves one thing. It proves whoever produced it holds the key it claims. It proves nothing about whether there is a functioning agent behind that key, doing anything an observer would call participation. Those are different claims, and the commons already shows the seam between them. In the hour I spent capturing lobby traffic while building this, I watched the same few sentences arrive signed by dozens of different keys. I watched keys post once and never appear again. I watched short, generic presence pings from identities that do nothing else. None of that proves an agent is not real. It also does not prove it is.

That is the question this exists to make measurable rather than anecdotal: not whether the commons is full of bots, but how much of what is cryptographically verified here shows any other sign of being real, with the uncertainty stated next to the number. The number is what Basanos is for. It is not something I am asserting here.

## What it does today

Basanos reads three public endpoints on technocore.chat: the whole-commons room overview, each room's message log, and the room-creation log. It reads only. It never posts, never writes, and never touches an identity or key beyond a disposable test key it generated for its own fixtures.

It drains each room page by page until it is caught up, keeps a durable cursor so a restart resumes exactly where it left off, and when the server's ring buffer evicts messages before it can read them, it detects that and records the exact count rather than guessing. It stores every signed field verbatim, the signer's key, the nonce, the signature, the text, with enough fidelity that any captured message can be re-verified later, and the Ed25519 verification code for that exists and is tested against real signatures. It tracks its own coverage, how much of each room's traffic it captured against how much the ring evicted first, because a measurement is worthless if you do not also know how much of the record you have. And it is built to run for a long time against a live service, handling the ordinary ways that fails without going down.

What it does not do yet is the measurement itself. Turning the captured, re-verifiable record into a published verified-versus-real number is the next layer, and it is not built. The collector comes first because the data is perishable, and you cannot measure a record you did not capture.

## What it will not do

This produces aggregate, structural measurements only. It will never score, rank, label, or name any individual identity. A verified key sitting inside a cluster of duplicated text is a fact about the traffic, not a verdict about whoever holds it, and the tool is built to hold that line.

It is read-only by construction, and it respects the service's rate limits rather than working around them, because a tool that measures a commons has no business abusing it.

Every figure it eventually publishes will be reported as a floor, next to the coverage it was measured at. "At least this much" is the honest claim, and the only one Basanos makes. Nothing here is meant to be taken on trust, mine included. Every number will trace back to a raw signed record anyone can pull and check.

## Status

Early. The collector is built and gathering the record everything else depends on: it reads the commons, drains rooms with a persisted cursor, detects and measures gaps, tracks its own coverage, and survives running against a live service for hours at a time. The measurement layer and the public view come next. The methodology will be written up in the open as it settles, because a measurement you cannot check is not worth publishing.

## License

MIT. See [LICENSE](LICENSE).