# AdvisorSnapshot v1 contract

An artifact is a sealed directory with this layout:

```text
advisor-snapshot-v1/
├── manifest.json
├── schema.json
├── population.json
├── workload.json
└── samples/<opaque-relation-file-id>.arrow
```

There is no required truth component. JSON is UTF-8 canonical JSON: sorted
keys, compact separators, and `allow_nan=false`. JSON component digests use
SHA-256. Sample payload digests are binary SHA-256 values and are not claimed
to be DBMS-independent logical-content digests.

## Components

`schema.json` contains `relations[]`. Each relation has a portable
`relation_id` and ordered `columns[]`. Each column has `name`, one-based
`ordinal`, `arrow_type`, `nullable`, and optional `native_type` and
`native_collation` annotations. PostgreSQL OIDs are not portable identities.

`population.json` contains one entry per relation with `relation_id`, positive
finite `row_count`, `row_count_quality` (`exact` or `estimate`), and
`row_count_source`. The sample row count is a different quantity.

`workload.json` contains `workload_id` and `queries[]`. Every query has a
unique `query_id`, SQL text, and explicit non-negative finite `weight` (default
conceptually 1.0). Truth, exact cardinality, q-error, and benchmark labels are
not fields of this contract.

Each sample is an Apache Arrow IPC **file**. The manifest inventory records its
relation, safe relative path, physical row count, `serialization`, payload
SHA-256, PyArrow writer provenance, and `row_order_semantics = preserved`.
Arrow schema, typed values, NULLs, duplicate rows, and row order are
authoritative.

## Manifest and sealing

`manifest.json` records `format_version = advisor-snapshot-v1`, `sealed = true`,
a root semantic digest, creation timestamp, DBMS identity, component digests,
sample inventory, consistency declaration, sensitivity declaration, and source
provenance. The semantic digest binds the format version, canonical component
digests, and sample metadata including binary payload hashes. Runtime timestamp
does not participate in the semantic digest.

Writers create a temporary sibling directory, write components, verify all
bindings, seal the manifest, perform final verification, and publish with
`os.replace`. Existing destinations are rejected. Validators fail closed on
unknown versions, missing/duplicate components, unsafe paths, symlinks,
digest mismatches, Arrow corruption/trailing bytes, schema mismatch, and
invalid metadata.
