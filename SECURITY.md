# Security

Steer controls a real desktop. Treat every finding as high impact.

- Report privately to the repository owner; do not open a public issue for a vulnerability.
- Never commit API keys, traces, cache files or screenshots. `.env`, `traces/` and `cache/` are gitignored.
- The safety gate (hard rules, risk tiers, provenance) is the trust boundary. A change that weakens it
  needs an explicit design-doc update and a second review.
