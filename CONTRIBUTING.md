# Contributing

Use Python 3.11, install `requirements-dev.txt`, and run `python -m pytest -q`
and `node tests/dashboard.test.cjs` before proposing a change.

Use synthetic account names, instance domains under `.example`, status IDs,
URIs, and tokens in tests. Never commit populated environment files,
production database copies, user posts, recovery journals, access tokens,
or logs from a live deployment. Keep API calls behind substituted network
boundaries in tests.

Preserve the public-only source filter, durable record keys, immutable reply
contexts, per-part checkpoints, and activation cutoffs. Add a meaningful
regression test when changing publication, retries, privacy, or threading.

Document behavior changes and any remaining limits. Prefer documented API
methods and maintained libraries. Keep implementation independent of other
products' proprietary code and branding.
