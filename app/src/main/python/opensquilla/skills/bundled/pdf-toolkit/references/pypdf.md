# pypdf reference

The structural side of PDF work — pages, metadata, forms, encryption.
Full API: <https://pypdf.readthedocs.io/>.

## Reading

```python
from pypdf import PdfReader
reader = PdfReader("doc.pdf")
print(len(reader.pages))
print(reader.metadata.title)
text = reader.pages[0].extract_text()
```

`extract_text()` is fast but layout-naive. For column-heavy docs use
`pdfplumber` (it has a richer text extractor).

## Writing

```python
from pypdf import PdfWriter
writer = PdfWriter()
writer.append("a.pdf")              # whole file
writer.append("b.pdf", pages=[0, 2, 4])
with open("merged.pdf", "wb") as fh:
    writer.write(fh)
```

`PdfWriter.append()` accepts page lists and ranges; `add_page()` accepts a
single `PageObject`.

## Page operations

```python
page = reader.pages[0]
page.rotate(90)                     # returns rotated page
writer.add_page(page)

# Crop
page.mediabox.upper_right = (612, 792)  # Letter
```

## Compression

```python
for page in writer.pages:
    page.compress_content_streams()
```

Reduces file size by 20-40% with minimal loss. Slow on large PDFs; budget
~50ms/page.

## Encryption

```python
reader = PdfReader("locked.pdf")
if reader.is_encrypted:
    reader.decrypt("password")
```

To encrypt:

```python
writer.encrypt(user_password="...", owner_password="...", algorithm="AES-256")
```

## Forms (AcroForm)

```python
fields = reader.get_fields()
# returns dict: {field_name: {/FT, /V, /DV, /Kids, ...}}

writer.update_page_form_field_values(
    writer.pages[0],
    {"applicant_name": "Wei E.", "agreed": "Yes"},
)
```

For checkboxes, the export value (often `Yes` or `On`, but theme-specific)
is the only valid input. Inspect with `get_fields()` first.

XFA forms are not supported by pypdf. Detect with `reader.trailer["/Root"]
.get("/AcroForm", {}).get("/XFA")`.

## Metadata

```python
writer.add_metadata({
    "/Title": "Q3 Review",
    "/Author": "Wei E.",
    "/Producer": "OpenSquilla pdf-toolkit",
})
```

## Pitfalls

- `PdfWriter` always writes a fresh PDF; it does not preserve byte-level
  layout. Use this for content-level transforms; for byte-level signatures
  preservation, look at `pdf-lib` (Node) or qpdf (CLI).
- `PdfReader.pages` is a lazy sequence — slicing materializes pages. Avoid
  `list(reader.pages)` on huge PDFs.
- Combining encrypted and unencrypted pages into one writer raises an
  exception; decrypt the source first.
