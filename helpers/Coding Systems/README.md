# IEEE 11073 Table Export

`export_11073_tables.py` extracts every table PyMuPDF detects from the local IEEE 11073 coding-system PDFs. It is a lookup and review aid, not a substitute for the source standards: table reconstruction from PDFs is inherently best-effort, and every exported row retains its source document, PDF page, table position, raw column names, and raw cell values.

> [!WARNING]
> This is not tested properly at all.
>
> Do not use this as only source of truth for any interoperability work. Always check the source standard as well.

## Install

```powershell
.venv\Scripts\python.exe -m pip install -r helpers\requirements.txt
```

## Export the local standards

```powershell
.venv\Scripts\python.exe helpers\export_11073_tables.py `
  --input "C:\Users\yourname\somefolderslike\ISO-IEEE-11073\CodingSystems" `
  --output helpers\output
```

Use `--csv-only` to skip the Excel workbook. Use `--pages 860-990` while tuning extraction against a subset of one document.

## Outputs

> [!NOTE]
> Output files won't be published here. They are protected by copyright, and you must obtain the source PDFs from IEEE or ISO to use this script yourself.
> Also, you should probably think twice before sharing the resulting Tables with someone else.

| File                             | Contents                                                                                                                                                                  |
|----------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `codes.csv`                      | Strict lookup rows with both an exact MDC reference ID and a valid `Part::Code`, reconstructed from the PDF text line. The original line remains in `source_values_json`. |
| `table_rows.csv`                 | Every row of every detected table, preserving every detected source column.                                                                                               |
| `tables.csv`                     | Table titles, headers, positions, pages, and row counts.                                                                                                                  |
| `documents.csv`                  | Source paths, hashes, page counts, and extractor version.                                                                                                                 |
| `issues.csv`                     | Pages or tables the extractor could not read or classify confidently.                                                                                                     |
| `ieee-11073-coding-systems.xlsx` | The above tables in separate worksheets.                                                                                                                                  |

For a specific term, filter `codes.csv` or the **Codes** worksheet by
`reference_id`, `common_term`, `systematic_name`, `part_code`, or any source column retained in the JSON fields. The row's `source_pdf_page` and
`source_filename` lead back to the standard.

## Limitations

- The 10101 amendments describe insertions and replacements. Their rows are exported with their own source document. They are not silently merged into the 2020 base edition.
- A page with no machine-readable text is logged to `issues.csv`. The script does not use OCR or invent values, because OCR mistakes in reference IDs and numeric codes would make the lookup unsafe.
- `table_rows.csv` is a lossless best-effort PDF-table extraction. Its raw source columns may be fragmented by the PDF's layout. Use it for complete coverage and inspect the cited PDF page where a table field matters. The stricter `codes.csv` is the quick lookup dataset and uses the less-fragmented text-line representation for its source row.
- Review `issues.csv` and spot-check the source pages before relying on an extracted value for interoperability work.
