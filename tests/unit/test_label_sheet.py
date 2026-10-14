"""Label sheet layout: 21-up A4 sticker sheets (3 x 7 labels of 63.5 x 38.1 mm)."""

import pytest

from app import label_sheet
from app.label_sheet import Cell, layout


def test_geometry_fills_a4_exactly():
    s = label_sheet
    gap = s.PITCH_X - s.LABEL_WIDTH
    assert pytest.approx(210) == s.COLS * s.LABEL_WIDTH + (s.COLS - 1) * gap + 2 * s.MARGIN_LEFT
    assert pytest.approx(297) == s.ROWS * s.PITCH_Y + 2 * s.MARGIN_TOP
    assert s.PITCH_Y == s.LABEL_HEIGHT


def test_all_21_cells_of_a_full_sheet():
    cells = layout(21)
    xs = [7.25, 73.25, 139.25]
    ys = [15.15, 53.25, 91.35, 129.45, 167.55, 205.65, 243.75]
    assert cells == [
        Cell(0, row, col, xs[col - 1], ys[row - 1]) for row in range(1, 8) for col in range(1, 4)
    ]
    last = cells[-1]
    assert last.x + label_sheet.LABEL_WIDTH == pytest.approx(210 - 7.25)
    assert last.y + label_sheet.LABEL_HEIGHT == pytest.approx(297 - 15.15)


def test_start_offset_leaves_earlier_cells_blank():
    cells = layout(10, start_row=3, start_col=2)
    assert [(c.row, c.col) for c in cells[:3]] == [(3, 2), (3, 3), (4, 1)]
    assert (cells[0].x, cells[0].y) == (73.25, 91.35)
    assert (cells[-1].sheet, cells[-1].row, cells[-1].col) == (0, 6, 2)


def test_overflow_continues_on_the_next_sheet_from_its_first_cell():
    cells = layout(10, start_row=7, start_col=3)
    assert (cells[0].sheet, cells[0].row, cells[0].col) == (0, 7, 3)
    assert [(c.sheet, c.row, c.col) for c in cells[1:4]] == [(1, 1, 1), (1, 1, 2), (1, 1, 3)]
    assert cells[-1].sheet == 1
    assert {c.sheet for c in layout(100)} == {0, 1, 2, 3, 4}
    assert len([c for c in layout(43) if c.sheet == 2]) == 1


def test_calibration_offset_shifts_every_label():
    plain, shifted = layout(21), layout(21, dx=-1.25, dy=2.5)
    for a, b in zip(plain, shifted, strict=True):
        assert (b.x, b.y) == (pytest.approx(a.x - 1.25), pytest.approx(a.y + 2.5))


@pytest.mark.parametrize(
    ("start_row", "start_col", "dx", "dy"),
    [(0, 1, 0, 0), (8, 1, 0, 0), (1, 0, 0, 0), (1, 4, 0, 0), (1, 1, 5.25, 0), (1, 1, 0, -6),
     (1, 1, 0.1, 0), (1, 1, float("nan"), 0)],
)  # fmt: skip
def test_bounds(start_row, start_col, dx, dy):
    with pytest.raises(ValueError):
        layout(1, start_row, start_col, dx, dy)


def test_offset_limits_are_inclusive():
    assert layout(1, dx=5, dy=-5)[0].x == pytest.approx(12.25)
