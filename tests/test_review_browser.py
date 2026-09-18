"""Browser integration: review → CSV → regenerated HTML → CSV, using synthetic data.

Run with NODE_PATH pointing to an installed Playwright package and
PLAYWRIGHT_BROWSERS_PATH pointing to its Chromium installation.
"""

import csv
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.review.make_review_html import RUBRIC, build_html, load_rows


@pytest.mark.skipif(
    not os.environ.get("NODE_PATH") or not shutil.which("node"),
    reason="Headless browser integration requires Node and Playwright in NODE_PATH",
)
def test_answers_survive_csv_html_browser_round_trip(tmp_path):
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"messages": []}))
    row = {
        "patient_id": "synthetic",
        "nudge_index": "0",
        "trace_path": "trace.json",
        "patient_summary": "Synthetic review fixture.",
        "generated_nudge_type": "lab_review",
        "urgency": "info",
        "category": "review",
        "grounding": "clinical_reasoning",
        "title": "Review the recorded finding",
        "description": "Consider reviewing this synthetic finding.",
        "rationale": "This is an integration test.",
        "guideline_citation": "",
        **{rid: "" for rid, _, _ in RUBRIC},
        "comment": "",
    }
    source = tmp_path / "input.csv"
    with source.open("w") as file:
        writer = csv.DictWriter(file, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    browser_script = tmp_path / "roundtrip.cjs"
    browser_script.write_text(
        """
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const [pagePath, outputPath, mode] = process.argv.slice(2);
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('file://' + pagePath);
  await page.locator('#begin-btn').click();
  if (mode === 'answer') {
    for (const item of await page.locator('[data-rid]').all()) {
      const id = await item.getAttribute('data-rid');
      await item.locator('[data-val="' + (id === 'nudge_type' ? 'clinical' : 'yes') + '"]').click();
    }
  }
  assert.equal(await page.locator('#prog-n').textContent(), '1');
  await page.reload();
  assert.equal(await page.locator('#prog-n').textContent(), '1');
  const pending = page.waitForEvent('download');
  await page.locator('#export-btn').click();
  await (await pending).saveAs(outputPath);
  assert.deepEqual(errors, []);
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
"""
    )
    template = Path(__file__).resolve().parents[1] / "evals/review/review_template_vC.html"
    for iteration, mode in enumerate(("answer", "preserve")):
        html = tmp_path / f"review-{iteration}.html"
        html.write_text(build_html(load_rows(source), template, tmp_path))
        exported = tmp_path / f"export-{iteration}.csv"
        subprocess.run(
            ["node", str(browser_script), str(html), str(exported), mode],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        with exported.open() as file:
            answers = list(csv.DictReader(file))
        assert len(answers) == 1
        assert answers[0]["generated_nudge_type"] == "lab_review"
        for rid, _, _ in RUBRIC:
            assert answers[0][rid] == ("clinical" if rid == "nudge_type" else "yes")
        source = exported
