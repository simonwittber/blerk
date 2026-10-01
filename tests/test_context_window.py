from __future__ import annotations

from blerk.symbols.types import (
    MARKER_END,
    MARKER_START,
    TRIMMED_PLACEHOLDER,
    _window_around_target,
)


def _ctx(before: int, target: int, after: int, width: int = 20) -> str:
    lines = [f"b{i:03d}".ljust(width, ".") for i in range(before)]
    lines.append(MARKER_START)
    lines += [f"t{i:03d}".ljust(width, ".") for i in range(target)]
    lines.append(MARKER_END)
    lines += [f"a{i:03d}".ljust(width, ".") for i in range(after)]
    return "\n".join(lines)


def test_short_context_is_untouched():
    text = _ctx(3, 3, 3)
    assert _window_around_target(text, 10_000) == text


def test_zero_budget_means_no_cap():
    text = _ctx(50, 5, 50)
    assert _window_around_target(text, 0) == text


def test_result_respects_the_budget():
    out = _window_around_target(_ctx(500, 5, 500), 2000)
    assert len(out) <= 2000 + 2 * (len(TRIMMED_PLACEHOLDER) + 1)


def test_target_is_kept_whole():
    out = _window_around_target(_ctx(500, 10, 500), 2000)
    for i in range(10):
        assert f"t{i:03d}" in out
    assert MARKER_START in out and MARKER_END in out


def test_nearest_lines_are_kept_on_both_sides():
    out = _window_around_target(_ctx(500, 5, 500), 2000)
    assert "b499" in out, "the line right before the target must survive"
    assert "a000" in out, "the line right after the target must survive"
    assert "b000" not in out and "a499" not in out, "the far ends must be trimmed"


def test_trim_is_marked_where_lines_were_dropped():
    out = _window_around_target(_ctx(500, 5, 500), 2000)
    lines = out.split("\n")
    assert lines[0] == TRIMMED_PLACEHOLDER
    assert lines[-1] == TRIMMED_PLACEHOLDER


def test_one_sided_context_spends_budget_on_the_other_side():
    out = _window_around_target(_ctx(0, 5, 500), 2000)
    assert not out.startswith(TRIMMED_PLACEHOLDER)
    assert out.count("\na") > 50


def test_oversized_target_keeps_its_opening_lines():
    out = _window_around_target(_ctx(10, 400, 10), 2000)
    assert "t000" in out, "the signature end of the target must survive"
    assert "t399" not in out
    assert out.rstrip().endswith(MARKER_END)
    assert len(out) <= 2000


def test_missing_markers_fall_back_to_a_plain_cut():
    text = "x" * 5000
    assert _window_around_target(text, 1000) == "x" * 1000
