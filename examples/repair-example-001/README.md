# Synthetic repair example 001

A small fixture repairs a missing `enabled` configuration flag. The source and recipe are synthetic. Reproduction runs the digest-pinned image in a network-disabled disposable container and compares the result tree root.

```sh
prf-dev reproduce repair-example-001 --isolated
```

This example currently demonstrates repeatable fixture output. It does not claim that protected repair authorization, historical source completeness, or publication has been proven. The general verifier accepts signed bundles using an independently supplied trust policy; production-ready PRO-90 interoperability still depends on the upstream contract being finalized.
