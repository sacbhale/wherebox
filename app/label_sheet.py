"""Layout of 21-up A4 sticker sheets (3 x 7 labels of 63.5 x 38.1 mm, like Avery L7160).

All positions are in mm from the sheet's top-left corner."""

from dataclasses import dataclass

COLS = 3
ROWS = 7
PER_SHEET = COLS * ROWS
LABEL_WIDTH = 63.5
LABEL_HEIGHT = 38.1
MARGIN_LEFT = 7.25
MARGIN_TOP = 15.15
PITCH_X = 66.0
PITCH_Y = 38.1
OFFSET_MAX = 5.0  # printer calibration: dx/dy within +-5 mm, in 0.25 mm steps
OFFSET_STEP = 0.25


@dataclass(frozen=True)
class Cell:
    sheet: int  # 0-based sheet (printed page) number
    row: int  # 1..ROWS
    col: int  # 1..COLS
    x: float  # label's left edge, mm
    y: float  # label's top edge, mm


def valid_offset(value: float) -> bool:
    """A calibration offset: within +-OFFSET_MAX and a whole number of OFFSET_STEPs."""
    steps = value / OFFSET_STEP
    return abs(value) <= OFFSET_MAX and steps == round(steps)


def layout(
    count: int, start_row: int = 1, start_col: int = 1, dx: float = 0.0, dy: float = 0.0
) -> list[Cell]:
    """Cells for ``count`` labels: the first at (start_row, start_col) of the first sheet (the
    cells before it stay blank), then left to right, top to bottom, onto further sheets as
    needed. ``dx``/``dy`` shift every label to compensate printer offset."""
    if not (1 <= start_row <= ROWS and 1 <= start_col <= COLS):
        raise ValueError("start cell is outside the sheet")
    if not (valid_offset(dx) and valid_offset(dy)):
        raise ValueError("offset must be within +-5 mm in 0.25 mm steps")
    first = (start_row - 1) * COLS + start_col - 1
    cells = []
    for index in range(first, first + count):
        sheet, slot = divmod(index, PER_SHEET)
        row, col = divmod(slot, COLS)
        x = round(MARGIN_LEFT + col * PITCH_X + dx, 2)
        y = round(MARGIN_TOP + row * PITCH_Y + dy, 2)
        cells.append(Cell(sheet, row + 1, col + 1, x, y))
    return cells
