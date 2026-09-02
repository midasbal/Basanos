# tests/fixtures/

Every `*.fixture.json` file here is **synthetic**, generated deterministically
by [`make_fixtures.py`](make_fixtures.py) — run `python tests/fixtures/make_fixtures.py`
to regenerate them, byte-identical every time.

Signed records are signed by a reproducible **throwaway fixture key**, derived
in `make_fixtures.py` from a fixed, hardcoded seed. It is a zero-value test
vector, unrelated to any real identity or to `~/.technocore-id` — chosen
specifically so that no real `did:key:` identity is ever pinned into this
repository's (public) git history.

Live-shape fidelity against the actual service is the live smoke test's job
(`collector/cli.py --once`), not these committed fixtures — if the real API
shape ever drifts, these files won't notice, and that's an accepted tradeoff
for never committing real captured data.

`rate_limited_429.synthetic.json` is unrelated (hand-authored, no signing, no
identity involved — a 429 response shape confirmed against the public
service's source) and isn't part of the generator.
