"""Offline regressions for reference templates and header-view object ownership."""

from __future__ import annotations

import gc
import sys

import pytest


def test_reference_templates_reject_private_object_access() -> None:
    fsspec = pytest.importorskip("fsspec")
    pytest.importorskip("jinja2")
    from jinja2.exceptions import SecurityError

    ordinary = fsspec.filesystem(
        "reference",
        fo={
            "version": 1,
            "refs": {},
            "gen": [{
                "key": "part-{{ i }}",
                "url": "memory://part-{{ i }}",
                "dimensions": {"i": [0]},
            }],
        },
        simple_templates=False,
        skip_instance_cache=True,
    )
    assert ordinary.references == {"part-0": ["memory://part-0"]}
    # Accessing class metadata is sufficient to exercise the sandbox boundary;
    # the fixture performs no process, filesystem, or network operations.
    with pytest.raises(SecurityError):
        fsspec.filesystem(
            "reference",
            fo={
                "version": 1,
                "refs": {},
                "gen": [{
                    "key": "{{ ''.__class__.__name__ }}",
                    "url": "memory://part",
                    "dimensions": {"i": [0]},
                }],
            },
            simple_templates=False,
            skip_instance_cache=True,
        )


@pytest.mark.skipif(sys.implementation.name != "cpython", reason="CPython reference ownership")
@pytest.mark.parametrize("operation", ["reflected-union", "subtraction"])
def test_header_items_set_operations_release_operand_references(operation: str) -> None:
    multidict = pytest.importorskip("multidict")
    headers = multidict.CIMultiDict({"seed": "fixed"})
    value = object()
    operand = [(f"header-{index}", value) for index in range(32)]
    gc.collect()
    before = sys.getrefcount(value)
    result = (
        operand | headers.items()
        if operation == "reflected-union"
        else headers.items() - operand
    )
    del result
    gc.collect()
    assert sys.getrefcount(value) == before
