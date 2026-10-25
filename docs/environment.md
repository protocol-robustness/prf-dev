# Environment profiles

Profiles check capabilities and record component-owned dependency inputs. They do not define replacement lockfiles.

- `core`: Python, Git/JJ, Clojure canonical hashing and OpenSSH signature verification. Does not require Symphony, EVM tools, a prover, credentials, or service access.
- `evm`: core plus Node/npm, Foundry and Anvil, with deployment pins read from protocol-deployments.
- `proving`: EVM plus Rust and prover tools, with pins read from the coprocessor repository. Hardware differences are capabilities, not global version substitutions.

`doctor` only observes tool presence and configured repositories. It does not install tools or certify later executions. Component repositories remain authoritative for their own versions.
