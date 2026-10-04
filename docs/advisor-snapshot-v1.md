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

There is no truth component in this artifact; optional production truth is a
separate GroundTruthSet. JSON is UTF-8 canonical JSON: sorted
keys, compact separators, and `allow_nan=false`. JSON component digests use
SHA-256. Sample payload digests are binary SHA-256 values and are not claimed
to be DBMS-independent logical-content digests.

## Components

`schema.json` contains `relations[]`. Each relation has a restricted opaque
artifact `relation_id`, a structured `relation_name` object with optional
`catalog` and `schema` plus required `name`, and ordered `columns[]`. Relation
and column names are preserved exactly; no `schema.table` concatenation or
database-specific identifier grammar is used. Each column has `name`, one-based
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
opaque relation ID, safe relative path, positive physical row count,
`serialization`, payload SHA-256, PyArrow writer provenance, and
`row_order_semantics = preserved`.
Arrow schema, typed values, NULLs, duplicate rows, and row order are
authoritative.

## Manifest and sealing

`manifest.json` records `format_version = advisor-snapshot-v1`, `sealed = true`,
a root semantic digest, creation timestamp, DBMS identity, component digests,
sample inventory, structured consistency declaration, semantic provenance,
runtime metadata, and sensitivity declaration.

The semantic manifest object is exactly the canonical object containing:

- `format_version`;
- `dbms`;
- `component_digests` for schema, population, and workload;
- `sample_inventory`, including payload hashes;
- `snapshot_consistency`;
- `semantic_provenance`; and
- `sensitivity`.

`created_at`, `sealed` (a validation gate), and `runtime_metadata` are
explicitly non-semantic. Any mutation to a semantic field changes the root
digest; an invalid consistency mode is rejected independently. Sensitivity and
interpretation-affecting provenance are therefore not silently mutable.

The v1 consistency object is:

```json
{
  "mode": "consistent-source-view",
  "db_derived_components": ["schema", "population", "samples"],
  "workload_source": "external"
}
```

It expresses one consistent source view for DB-derived components without
encoding PostgreSQL transaction syntax. A v1 workload is externally supplied.

Samples must contain at least one row, and a workload must contain at least one
positive-weight query; zero-weight queries may coexist with positive queries.

Writers create a temporary sibling directory, write components, verify all
bindings, seal the manifest, perform final verification, and publish with
`os.replace`. Existing destinations are rejected. Validators fail closed on
unknown versions, missing/duplicate components, unsafe paths, symlinks,
digest mismatches, Arrow corruption/trailing bytes, schema mismatch, and
invalid metadata.
