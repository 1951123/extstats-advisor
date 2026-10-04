from __future__ import annotations

import inspect

from extstats_advisor.dbms.base import AcquisitionInputs, DBMSAcquirer, SampleRequest


def test_dbms_contract_is_small_and_backend_neutral() -> None:
    assert inspect.signature(DBMSAcquirer.acquire).parameters.keys() == {
        "self",
        "relations",
        "requests",
    }
    request = SampleRequest("public.events", 1000, "fixed-random", seed=7)
    assert request.relation_id == "public.events"
    assert request.seed == 7
    assert AcquisitionInputs.__dataclass_fields__["samples"].type == "Mapping[str, Any]"
