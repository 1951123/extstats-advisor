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

The upcoming PostgreSQL deployment/preflight stage is governed by the
versioned policy `postgresql-add-only-deployment-v1`. Advisor-managed
statistics are exactly the objects explicitly described by the current
Recommendation. Every other extended-statistics object already present in
production is externally managed, whether it was created by a DBA, an
application team, another tool, an older advisor run, or manual experimentation.
The advisor does not model, reconcile, replace, rename, or remove those
external objects. Reconciliation remains the DBA/operator's responsibility.

Arbitrary existing production extended statistics are not a preflight blocker
and may coexist with the current Recommendation. Deployment v1 may create and
configure the recommended objects and must not automatically drop, alter,
rename, replace, or garbage-collect unrelated existing objects. A collision
with a deterministic schema/name requested by the current Recommendation is a
different case: preflight must fail closed for operator intervention, even if
the existing definition appears compatible. It must not hide the collision
with `IF NOT EXISTS` or silently drop the object.

The physical order contract is
`postgresql-statistics-oid-order-v1`. PostgreSQL 16's extended-statistics list
is ordered by physical OID, and exact estimator ties can depend on that list.
Deployment creates objects sequentially in the recommendation's
`deployment_ordered_candidate_ids`, inspect assigned OIDs, and verify the
required relative order before commit. If `F` is the SingletonProfile frozen
global precedence and `M*` is the SearchResult selected membership, the
Recommendation order is `D = F|_(M*)`. Verification is responsible only for
the selected Recommendation objects: for `D = [A, B, C]`,
`OID(A) < OID(B) < OID(C)` must hold, while externally managed objects may be
interleaved anywhere in the global OID sequence. Deployment must not require
all production extended statistics to equal the Recommendation set or order.
The transactional implementation records the managed OIDs and verifies this
relative order before and after commit.

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
the newly recommended extended statistics. PostgreSQL may also rebuild data
for externally managed extended statistics on the same relation; that behavior
does not transfer ownership to the advisor. `no-change` recommendations
contain no DDL and no ANALYZE. A recommendation is not a deployment result.

The optimizer evaluated the advisor membership `M*`, not necessarily the
combined production state `E_existing ∪ M*`. Therefore the SearchResult
objective must not be presented as a guaranteed objective after arbitrary
external statistics coexist with the Recommendation. The DBA decides whether
existing statistics remain, are removed, are replaced, or coexist. Future
policies such as `replace-previous-advisor-set` or
`managed-set-reconciliation` require a separate contract and are out of scope
for deployment v1.

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
