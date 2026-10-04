# Research boundary

This repository is not a benchmark harness. The following must not become core
dependencies or required artifact fields:

- exact cardinality truth or an oracle;
- q-error objectives or benchmark train/valid/test labels;
- RQ1/RQ2/RQ3/RQ4 drivers, random-order experiments, or dataset ETL;
- exact `COUNT(*)` population acquisition by default;
- execution of every workload query to obtain truth;
- benchmark-specific candidate catalogs, incidence artifacts, or milestone APIs;
- research Bundle compatibility layers and historical experiment drivers.

Research evaluation belongs in `pg-extstats-benchmarks`, which invokes this
repository as a system under test. A production workload may contain sensitive
SQL and sample values; the snapshot sensitivity declaration records that fact,
but this bootstrap does not provide encryption or anonymization.
