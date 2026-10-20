"""Source labels remain optional across older and newer Workbench clients."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.workbench_resources_get import (
    Result as WorkbenchResourcesGetResult,
)
from opensquilla.contracts.generated.v4.workbench_resources_list import (
    Result as WorkbenchResourcesListResult,
)
from opensquilla.contracts.generated.v4.workbench_resources_open import (
    Result as WorkbenchResourcesOpenResult,
)


@pytest.mark.parametrize(
    ("model", "wire", "resource"),
    [
        (
            WorkbenchResourcesGetResult,
            {"resource": {"resource": {"type": "document", "documentId": "doc-a"}}},
            "resource",
        ),
        (
            WorkbenchResourcesListResult,
            {"resources": [{}], "totalCount": 1, "pageSize": 1, "returnedCount": 1,
             "hasMore": False, "nextCursor": None},
            "resources",
        ),
        (
            WorkbenchResourcesOpenResult,
            {"disposition": "document", "resolution": {"status": "current"},
             "resource": {}, "materialized": False},
            "resource",
        ),
    ],
)
def test_optional_source_path_preserves_wire_and_rejects_invalid_values(model, wire, resource):
    assert model.model_validate(wire).model_dump(mode="json", exclude_unset=True) == wire
    for value in ["original/report.docx", "", "x" * 4097, 123, None]:
        example = deepcopy(wire)
        target = example[resource][0] if resource == "resources" else example[resource]
        target["sourcePath"] = value
        if value is None or value == "original/report.docx":
            assert model.model_validate(example).model_dump(
                mode="json", exclude_unset=True,
            ) == example
        else:
            with pytest.raises(ValidationError):
                model.model_validate(example)
