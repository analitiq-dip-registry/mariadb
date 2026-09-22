"""Pins the read/write DECIMAL scale bound in ``definition/type-map.json``.

The read-direction capture (``(?<scale>...)`` on the two DECIMAL/NUMERIC/FIXED
rules) and the write-direction capture (``(?<s>...)`` on the single
``DECIMAL(${p}, ${s})`` rule, ~280 lines below) are edited independently, and
``analitiq-validate`` cannot catch a mismatch between them: it only bounds a
capture group by Arrow's own Decimal128/256 precision ceiling (38), not by
MariaDB's actual DECIMAL scale ceiling (30, MariaDB's documented maximum). A
read rule that accepts a wider scale than the write rule can produce a
canonical value write can never render back out, silently breaking read/write
round-trip convergence for that column. This file pins the two bounds equal.

Read rules are matched in RE2 with a full match, on the probed native type
after normalization (strip, collapse whitespace, uppercase). Python ``re``
stands in for RE2 here: both directions' patterns use only literals, ``\\s``,
``\\d``, character classes, alternation, groups and anchors, which mean the
same in both dialects. The one syntactic difference is the named group
spelling — RE2 accepts ``(?<p>...)``, Python needs ``(?P<p>...)`` — so it is
translated before compiling.
"""

import json
import re
from pathlib import Path

import pytest

TYPE_MAP = json.loads(
    (Path(__file__).resolve().parent.parent / "definition" / "type-map.json").read_text(
        encoding="utf-8"
    )
)


def _normalize_native(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).upper()


def _read_arrow_type(native: str):
    """First-match-wins read lookup; None when no rule matches."""
    subject = _normalize_native(native)
    for rule in TYPE_MAP["read"]:
        if rule["match"] == "exact":
            if _normalize_native(rule["native_type"]) == subject:
                return rule["arrow_type"]
            continue
        pattern = rule["native_type"].replace("(?<", "(?P<")
        match = re.fullmatch(pattern, subject)
        if match:
            return re.sub(r"\$\{(\w+)\}", lambda m: match.group(m.group(1)), rule["arrow_type"])
    return None


_WRITE_DECIMAL_RULE = next(
    rule for rule in TYPE_MAP["write"] if rule.get("native_type") == "DECIMAL(${p}, ${s})"
)


def _write_side_accepts(arrow: str) -> bool:
    pattern = _WRITE_DECIMAL_RULE["arrow_type"].replace("(?<", "(?P<")
    return re.fullmatch(pattern, arrow) is not None


class TestDecimalScaleBoundsAgree:
    @pytest.mark.parametrize(
        "native, arrow",
        [
            ("DECIMAL(38,30)", "Decimal128(38, 30)"),
            ("NUMERIC(65,30)", "Decimal256(65, 30)"),
            ("FIXED(10,0)", "Decimal128(10, 0)"),
        ],
    )
    def test_in_range_declarations_map_and_write_side_accepts_them(self, native, arrow):
        assert _read_arrow_type(native) == arrow
        assert _write_side_accepts(arrow)

    @pytest.mark.parametrize(
        "native",
        [
            "DECIMAL(38,31)",
            "NUMERIC(65,31)",
        ],
    )
    def test_scale_31_is_out_of_read_range(self, native):
        # A read rule that still matched scale 31 would produce a canonical
        # value the write rule below rejects — the exact defect this pins.
        assert _read_arrow_type(native) is None

    def test_write_side_rejects_scale_31(self):
        assert not _write_side_accepts("Decimal128(38, 31)")
