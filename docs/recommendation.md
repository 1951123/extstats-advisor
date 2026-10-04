# PostgreSQL statistics recommendation

`statistics-recommendation-v1` is a deterministic desired-state artifact
consuming a validated `SearchResult`. It does not connect to PostgreSQL,
execute DDL, perform preflight, deploy, verify, or provide a rollback plan.

The ownership of membership and planner precedence is deliberately split:

```text
F   = SingletonProfile.frozen_ordered_candidate_ids
M*  = SearchResult selected membership
C*  = (M*, F restricted to M*)
```

Search chooses `M*`; it does not create a new planner order. Recommendation
independently derives `F restricted to M*` and fails closed if that result does
not equal the redundant `SearchResult.final_ordered_candidate_ids` field. It
also requires every selected candidate to be inside the plan's screened space
and PRESENT in the NativeStatsRepository.

The v1 PostgreSQL deployment contract is
`postgresql-extended-statistics-deployment-v1`. It supports exactly MCV and
dependencies statistics. Object names use
`postgresql-advisor-statistics-name-v1`: a stable SHA-256-derived identifier
from candidate ID and kind, with separate schema and name fields and no OIDs,
DSNs, credentials, runtime state, or deployment position.

The physical order contract is
`postgresql-statistics-oid-order-v1`. PostgreSQL 16's extended-statistics list
is ordered by physical OID, and exact estimator ties can depend on that list.
Future deployment must create objects sequentially in the recommendation's
`deployment_ordered_candidate_ids`, inspect assigned OIDs, and verify the
required relative order before commit. This unit does not perform that
verification.

For a non-empty proposed change, structured actions are authoritative and are
ordered as follows for every candidate in `F|M*`:

```text
CREATE STATISTICS
ALTER STATISTICS ... SET STATISTICS <NativeStatsRepository target>
...
ANALYZE "schema"."relation"
```

There is exactly one ANALYZE action, after all CREATE and ALTER actions. It
refreshes ordinary PostgreSQL statistics for the relation as well as building
extended statistics, so this recommendation is a material production
operation. `no-change` recommendations contain no DDL and no ANALYZE. A
recommendation is not a deployment result.

Budget-expired searches preserve their exact termination reason. A non-empty
last fully committed improving membership may produce `propose-change` after
`budget-expired-before-round` or `budget-expired-incomplete-round`; this does
not claim global optimality or local convergence. An empty membership produces
`no-change`.

The rendered SQL is a deterministic review-only representation of structured
actions. Every schema, relation, statistics object, and column identifier is
quoted and embedded double quotes are doubled. No `IF NOT EXISTS`, `DROP
STATISTICS`, or `CREATE OR REPLACE` is emitted. Dropping created extended
statistics later would not restore the ordinary-statistics state changed by
ANALYZE, so Recommendation v1 does not claim a complete rollback script.
