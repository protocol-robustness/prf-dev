# Repair evidence

The public schemas are versioned interoperability views intended to align with PRO-90. PRO-90 owns protected repair authorization, evidence semantics, independent review, and separate publication authority. PRO-106 owns routine agent submission/review/staging. Those upstream contracts remain authoritative; this toolkit does not replace them.

The record sequence is assessment, prior authorization, execution, independent review, verification, and (where separately authorized) publication receipt. The verifier requires an independently supplied trust policy; a report cannot nominate its own trusted keys. Role separation and scope are checked, and authority-bearing acceptance requires three distinct governance members in the external trust policy. Repair approval does not grant publication authority.

Verification is offline and non-executing. It recomputes PRF canonical roots, checks artifact digests and OpenSSH Ed25519 signatures, then emits claim-scoped statuses (`verified`, `failed`, `unknown`, `not-applicable`). Historical completeness and process identity beyond the signing credential remain `unknown` unless separate evidence establishes them.

The synthetic example demonstrates reproducible output. Its fixture keys, if used, are test-only and confer no production authority. A successful replay alone does not establish authorization, historical preservation, independent review, or publication.
