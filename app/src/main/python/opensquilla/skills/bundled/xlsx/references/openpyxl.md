# openpyxl reference

Cell types, formulas, formatting, and the half-dozen knobs that bite when
authoring `.xlsx` programmatically. Full API: <https://openpyxl.readthedocs.io/>.

## Cell value types

openpyxl stores native Python types and infers `data_type` on save:

| Python type | data_type | Notes |
|---|---|---|
| `int`, `float` | `n` (numeric) | |
| `str` not starting with `=` | `s` (string) | |
| `str` starting with `=` | `f` (formula) | Auto-detected at write time |
| `datetime`, `date` | `d` (datetime) | Excel renders per `number_format` |
| `bool` | `b` | |
| `None` | empty cell | |

To write a literal `=hello`, prefix with apostrophe: `cell.value = "'=hello"`.
Excel hides the leading apostrophe but preserves it across edits.

## Loading

```python
from openpyxl import load_workbook
wb = load_workbook("book.xlsx")                  # formulas as expressions
wb = load_workbook("book.xlsx", data_only=True)  # cached computed values
wb = load_workbook("book.xlsx", read_only=True)  # streaming, big files
```

`read_only=True` returns a workbook where you can iterate but not mutate.
Use it for inspect-only paths on >50MB workbooks.

## Iteration

```python
ws = wb["Sales"]
for row in ws.iter_rows(min_row=2, values_only=True):
    print(row)              # tuple of values

for row in ws.iter_rows():
    for cell in row:
        print(cell.coordinate, cell.value, cell.data_type)
```

`max_row` and `max_column` reflect the highest cell ever written, not the
highest currently populated. After deletes, openpyxl can return phantom rows.

## Formulas

Set with a leading `=`:

```python
ws["C2"] = "=B2*1.05"
ws["D2"] = "=SUM(A2:C2)"
```

openpyxl does **not** evaluate formulas. The cell renders as `0` or `#NAME?`
in Excel until Excel itself recomputes on open. Use a calc engine
(`pycel`, `formulas`, or LibreOffice headless) when you need values without
opening the workbook.

## Styles

```python
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
ws["A1"].font = Font(bold=True, size=12, color="FFFFFFFF")
ws["A1"].fill = PatternFill(start_color="FF1F2937", end_color="FF1F2937", fill_type="solid")
ws["A1"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
```

Apply styles **before** merging cells if you want the merged region to
inherit them — merging keeps the top-left cell's styles only.

## Number formats

```python
ws["B2"].number_format = '"$"#,##0.00'    # currency
ws["B3"].number_format = '0.0%'           # percent
ws["B4"].number_format = 'yyyy-mm-dd'     # date
ws["B5"].number_format = '#,##0'          # thousands
```

## Column widths and row heights

```python
ws.column_dimensions["A"].width = 18      # in character units, not pixels
ws.row_dimensions[1].height = 24          # in points
```

There is no auto-fit. Iterate cell values to compute width yourself, or
accept default widths.

## Freeze panes and split

```python
ws.freeze_panes = "A2"     # freeze row 1
ws.freeze_panes = "B2"     # freeze row 1 + column A
```

## Merged cells

```python
ws.merge_cells("A1:C1")
ws.unmerge_cells("A1:C1")
```

After merge, only the top-left cell holds a value; the others are empty.
Reading the bottom-right of a merged range returns `None`.

## Defined names

Workbook-scoped names live on `wb.defined_names`. Sheet-scoped on
`ws.defined_names`. Deleting a defined name without updating dependent
formulas yields `#NAME?` errors.

## Charts

openpyxl supports bar, line, pie, scatter, area, and bubble charts. The
chart references are sheet-relative; moving sheets after chart creation
requires manual fix-ups. For complex visuals prefer `xlsxwriter` (write-only,
richer chart API) at workbook authoring time.

## Pitfalls

- Saving over an open workbook on Windows raises `PermissionError`. Close
  Excel before save.
- `data_only=True` only returns cached values written by Excel; an `.xlsx`
  authored only by openpyxl has no cached values, so `data_only=True` returns
  `None` for every formula.
- Pivot caches are read but not written cleanly. Avoid mutating workbooks
  with pivots via openpyxl.
