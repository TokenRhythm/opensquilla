"""Pattern registry for meta-skill-creator."""

from pydantic import BaseModel

from opensquilla.skills.creator.patterns.schemas import (
    FanOutMergeSlots,
    SequentialSlots,
)

PATTERN_SLOT_SCHEMA: dict[str, type[BaseModel]] = {
    "p1_sequential": SequentialSlots,
    "p2_fan_out_merge": FanOutMergeSlots,
    "p3_condition_gated": SequentialSlots,
}
