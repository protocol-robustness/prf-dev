# Symphony interoperability

Symphony owns orchestration, dispatch, workspace lifecycle, reviewer execution, and staging. prf-dev does not run agents. The `prf-dev routine` pilot provides offline signed record creation and verification for adapters to consume, binding each record to exact candidate identities and an independent policy root.

This is not yet a production lifecycle integration: current Symphony coding, review, and staging lanes do not request these signatures, and the existing staging admission/promotion path does not require a routine staging receipt. Do not treat the pilot as a gate or describe unsigned legacy work as signed. Integration must add an isolated signer boundary and require the verified receipt before staging acceptance; signer unavailability must fail signed qualification closed without weakening existing protected-publication controls.
