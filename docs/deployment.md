# PostgreSQL deployment contract

The production deployment contract is
`postgresql-extended-statistics-deployment-v1`, governed by
`postgresql-add-only-deployment-v1`.

## Ownership and scope

The advisor manages only the extended-statistics objects named by the current
`Recommendation`. Arbitrary extended statistics already present in production
do not block preflight and are not modeled as part of the advisor source chain.
The advisor never automatically drops, replaces, renames, or alters an
unrelated object. The DBA or operator owns reconciliation between existing
production objects and a later Recommendation.

The one exception is a deterministic `(schema, name)` collision with a current
Recommendation object. That collision is fail-closed, including when the
existing definition looks compatible. Deployment never hides it with
`IF NOT EXISTS`.

The optimizer evaluated membership `M*`, not necessarily the combined
production state `E_existing ∪ M*`. Consequently, the SearchResult objective
must not be presented as a guaranteed objective for the combined production
state when externally managed statistics coexist. The DBA decides whether
those objects remain, are removed, replaced, or coexist.

The current Recommendation ordering is preserved as
`D = F|_(M*)`, where `F` is the frozen SingletonProfile precedence. Physical
verification checks only the relative OID order of advisor-managed objects.
Externally managed objects may be interleaved freely.

## Transaction lifecycle

For a non-empty `propose-change` Recommendation, deployment:

1. validates the sealed source chain before connecting;
2. connects with psycopg `autocommit=True` and executes one explicit
   `BEGIN`;
3. applies transaction-local `lock_timeout` and `statement_timeout` with
   `SELECT pg_catalog.set_config(..., %s, true)`;
4. locks the target table with `SHARE UPDATE EXCLUSIVE`, then checks database,
   server version, relation identity, selected columns, privileges, external
   statistics, and deterministic-name collisions;
5. executes only structured Recommendation CREATE/ALTER actions;
6. verifies the managed catalog rows before ANALYZE, executes exactly one
   `ANALYZE "schema"."relation"`, and verifies native payloads and managed
   relative OID order before COMMIT;
7. commits, then opens a fresh read-only transaction and repeats the managed
   verification.

Every pre-commit error rolls back and terminates the mutation connection. If
COMMIT succeeds but the fresh post-commit verification fails, deployment raises
an explicit committed-but-unverified error and writes no success artifact.

A `no-change` Recommendation performs source validation only: it opens no
production connection, issues no DDL or ANALYZE, and produces a deterministic
no-op result.

## ANALYZE disclosure

For non-empty deployment the final Recommendation action remains one
`ANALYZE`. It refreshes ordinary relation statistics, builds the newly
recommended extended statistics, and may also rebuild data for externally
managed extended statistics on the same relation. This does not transfer
ownership of those external objects to the advisor.

## Artifacts and commands

Successful deployment writes the canonical
`postgresql-deployment-result-v1` artifact only after post-commit verification.
It records the source digests, deployment contract and policy, target relation
and OID, server identity, decision, managed object identities, preflight,
commit, post-commit verification, and execution policy. It contains no DSN or
credential.

The CLI entry points are:

```text
extstats-advisor deployment preflight postgres ...
extstats-advisor deployment apply postgres ... --dsn DSN --output result.json
extstats-advisor deployment validate result.json ...
extstats-advisor deployment inspect result.json
```

Future policies such as `replace-previous-advisor-set` and
`managed-set-reconciliation`, including garbage collection, are outside this
contract.
