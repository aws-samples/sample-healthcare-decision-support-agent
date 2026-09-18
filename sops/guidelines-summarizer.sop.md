# Guidelines Summarizer SOP

## Overview

This SOP guides an AI agent to analyze medical guideline PDF documents and generate structured summaries for contextual retrieval. The outputs include a master catalog JSON and per-guideline markdown summaries that can be used to improve search relevance in OpenSearch.

## Parameters

- `pdf_path` (required): Path to PDF file or directory. Can be a local path (e.g., `guidelines/pdfs/ADA/`) or S3 URI (e.g., `s3://bucket/prefix/`)
- `output_dir` (optional): Output directory for generated files. Defaults to `guidelines/`
- `mode` (optional): Processing mode - `single` (one PDF), `batch` (directory of PDFs), or `update` (add to existing catalog). Defaults to `single`
- `aws_profile` (optional): AWS profile for S3 access. Defaults to the standard AWS credential chain

## Steps

### 1. Setup and Validation

1.1. Validate the `pdf_path` parameter exists:
   - If local path: MUST verify the file or directory exists
   - If S3 URI: MUST download to a temporary local directory using AWS CLI

1.2. Create output directories if they don't exist:
   - `{output_dir}/summaries/` for markdown summary files

1.3. Load existing `{output_dir}/catalog.json` if present to preserve previous entries when updating.

### 2. Analyze Guideline Document

For each PDF file to process:

2.1. Read the PDF content using **pdfplumber**. Run in an isolated environment to avoid dependency conflicts:

```bash
# Create isolated environment and extract text
cd /tmp && uv venv pdf_env && source pdf_env/bin/activate && uv pip install pdfplumber
```

```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    print(f"Total pages: {len(pdf.pages)}")

    # Extract text from all pages
    full_text = ""
    for page in pdf.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n\n"
```

For extracting tables (useful for recommendation summaries):

```python
with pdfplumber.open("document.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        for j, table in enumerate(tables):
            print(f"Table {j+1} on page {i+1}:")
            for row in table:
                print(row)
```

2.2. Extract document-level metadata:
   - **Title**: The official title of the guideline (e.g., "Standards of Care in Diabetes - 2026")
   - **Organization**: The publishing body (e.g., "American Diabetes Association", "AHA/ACC", "CDC")
   - **Year**: Publication year extracted from filename or content
   - **Target Population**: Who this guideline applies to (e.g., "Adults with Type 2 Diabetes", "Patients with cardiovascular disease")
   - **Conditions Covered**: List of medical conditions, diseases, or ICD-10 codes addressed
   - **Specialties**: Relevant medical specialties (e.g., "endocrinology", "cardiology", "primary_care")

2.3. Derive the `source` identifier from the filename:
   - Use organization abbreviation + year (e.g., "ADA 2026", "AHA_ACC 2023")
   - This MUST match the source used in OpenSearch indexing

### 3. Extract Key Sections

3.1. Identify major sections/chapters in the document. Look for:
   - Numbered sections (e.g., "Section 9: Pharmacologic Approaches")
   - Chapters with clear headers
   - Major topic divisions

3.2. For each key section, extract:
   - **Section Number**: The hierarchical number (e.g., "9", "9.2", "10.3.1")
   - **Section Title**: The section heading
   - **Summary**: 2-3 sentences describing what this section covers
   - **Key Recommendations**: Bulleted list of actionable clinical recommendations
   - **Target Population**: If this section applies to a specific subset (e.g., "patients with obesity")

3.3. The agent SHOULD focus on sections containing:
   - Treatment recommendations
   - Diagnostic criteria
   - Screening guidelines
   - Medication guidance
   - Risk assessment protocols

### 4. Generate Executive Summary

4.1. Create a concise executive summary (3-5 sentences) that:
   - Describes the scope and purpose of the guideline
   - Identifies the primary clinical focus areas
   - Notes any major updates or changes from previous versions (if mentioned)

4.2. This executive summary will be prepended to each chunk during OpenSearch indexing for contextual retrieval.

### 5. Generate Markdown Summary

5.1. Create a structured markdown file at `{output_dir}/summaries/{source}.md` with the following format:

```markdown
# {Title}

## Metadata
- **Organization:** {Organization}
- **Year:** {Year}
- **Specialties:** {Comma-separated list}

## Summary Generation
- **Created:** {ISO 8601 date, e.g., 2026-01-05}
- **Model:** Claude Sonnet 5 (any current model with PDF-length context works)
- **SOP Version:** {git commit hash, get via: `git log -1 --format=%H -- sops/guidelines-summarizer.sop.md`}

## Target Population
{Description of who this guideline applies to}

## Conditions Covered
{Bulleted list of conditions/ICD-10 codes}

## Executive Summary
{3-5 sentence summary}

## Key Sections

### Section {number}: {title}
**Summary:** {2-3 sentence summary}

**Key Recommendations:**
- {Recommendation 1}
- {Recommendation 2}
- ...

### Section {number}: {title}
...
```

5.2. The source identifier in the filename SHOULD use underscores instead of spaces (e.g., `ADA_2026.md`).

### 6. Update Catalog

6.1. Add or update an entry in `{output_dir}/catalog.json` with the following structure:

```json
{
  "source": "{source identifier}",
  "title": "{official title}",
  "organization": "{organization name}",
  "year": {publication year as integer},
  "target_population": "{target population description}",
  "specialties": ["{specialty1}", "{specialty2}"],
  "conditions_covered": ["{condition1}", "{condition2}"],
  "executive_summary": "{3-5 sentence summary}",
  "summary_path": "guidelines/summaries/{source}.md",
  "pdf_path": "{path to source PDF}",
  "summary_generated": {
    "date": "{ISO 8601 date}",
    "model": "Claude Sonnet 5",
    "sop_version": "{git commit hash}"
  }
}
```

6.2. The catalog JSON structure MUST be:

```json
{
  "version": "1.0",
  "generated_at": "{ISO 8601 timestamp}",
  "guidelines": [
    { ... entry 1 ... },
    { ... entry 2 ... }
  ]
}
```

6.3. When updating (`mode=update`):
   - If an entry with the same `source` exists, replace it
   - Otherwise, append the new entry
   - Sort entries alphabetically by `source`

### 7. Output Summary

7.1. After processing, output a summary:
   - Number of guidelines processed
   - Path to generated summary files
   - Path to updated catalog.json

## Examples

### Example 1: Process Single Local PDF

**Input:**
```
pdf_path: guidelines/pdfs/ADA/ada-standards-of-care-2026.pdf
output_dir: guidelines/
mode: single
```

**Expected Output:**
- Created: `guidelines/summaries/ADA_2026.md`
- Updated: `guidelines/catalog.json` with ADA 2026 entry

### Example 2: Batch Process S3 Directory

**Input:**
```
pdf_path: s3://medical-nudging-guidelines-123456789012/Guidelines-from-vendor/
output_dir: guidelines/
mode: batch
aws_profile: default
```

**Expected Output:**
- Downloaded PDFs from S3
- Created: `guidelines/summaries/ADA_2026.md`, `guidelines/summaries/AHA_ACC_2023.md`, etc.
- Updated: `guidelines/catalog.json` with all entries

## Troubleshooting

### PDF Reading Issues

**Always use pdfplumber** for PDF text extraction. If you encounter dependency conflicts with the project's virtual environment, use an isolated environment:

```bash
# Create isolated venv outside project directory
cd /tmp && uv venv pdf_env && source pdf_env/bin/activate && uv pip install pdfplumber

# Then run extraction
python3 -c "
import pdfplumber
with pdfplumber.open('/path/to/document.pdf') as pdf:
    for page in pdf.pages:
        print(page.extract_text())
"
```

**Alternative libraries** (if pdfplumber fails):
- `pypdf` - Basic text extraction and PDF manipulation
- `pdfminer.six` - Lower-level text extraction (pdfplumber uses this internally)

### Scanned PDFs (OCR Required)

For scanned PDFs without selectable text, use OCR:

```python
# Requires: pytesseract, pdf2image
import pytesseract
from pdf2image import convert_from_path

images = convert_from_path('scanned.pdf')
text = ""
for i, image in enumerate(images):
    text += f"Page {i+1}:\n"
    text += pytesseract.image_to_string(image)
    text += "\n\n"
```

### S3 Access Issues
- Ensure AWS credentials are configured for the specified profile
- The agent SHOULD use `AWS_PROFILE={aws_profile} aws s3 cp` or `aws s3 sync` commands

### Large PDFs
- For very large PDFs (>100 pages), process in chunks by page ranges
- Focus on executive summary, table of contents, and key recommendation sections
- Use page iteration to avoid memory issues:

```python
with pdfplumber.open("large_document.pdf") as pdf:
    # Process pages in batches
    for i in range(0, len(pdf.pages), 10):
        batch = pdf.pages[i:i+10]
        for page in batch:
            text = page.extract_text()
            # Process text...
```

## Quick Reference

| Task | Tool | Code |
|------|------|------|
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Get page count | pdfplumber | `len(pdf.pages)` |
| OCR scanned PDF | pytesseract | `pytesseract.image_to_string(image)` |
| Merge PDFs | pypdf | `writer.add_page(page)` |
