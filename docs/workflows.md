# Workflow ownership

`prf-dev` provides shared, portable inspection and verification entry points. It delegates component facts to owning repositories.

- `workflows/jj`: snapshot and report JJ commit/tree/operation observations. Operation IDs are local historical evidence; commit and tree IDs bind portable source identity.
- `workflows/symphony`: define the boundary for Symphony-owned submission, review, staging, and the new offline routine signing pilot; orchestration remains in Symphony and automatic signature enforcement is not yet deployed.
- `workflows/integration`: consume component-owned staging admission evidence, qualify an exact candidate, and preserve reviewed-candidate/integrated-result bindings. Integration authority remains with the owner of protected refs.

See PRO-74 for cross-repository command ownership, PRO-90 for protected repair, PRO-106 for routine submissions, review and staging, and PRO-111 for the staging-to-integration promotion gate. These issues are not represented as completed by this toolkit.
