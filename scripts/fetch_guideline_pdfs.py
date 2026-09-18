#!/usr/bin/env python3
"""Best-effort fetcher for the guideline corpus described in guidelines/sources.json.

Downloads each source's PDF into guidelines/pdfs/<SOURCE_KEY>.pdf and writes a
ledger (guidelines/pdfs/_ledger.json) recording, per source, which route worked
or why it failed.

The PDFs are NOT redistributable (see guidelines/sources.json "licensing") and
guidelines/pdfs/ is gitignored. This script only automates retrieval of
documents that are free to read.

Route notes learned empirically (2026-08):
  * cdc.gov and nih.gov 403 a bare curl/requests User-Agent but serve normally
    when the full Chrome header set (sec-ch-ua, Sec-Fetch-*, Accept-Language)
    is present. A UA string alone is not enough.
  * pmc.ncbi.nlm.nih.gov /pdf/ serves a JavaScript proof-of-work challenge.
    Europe PMC's ?pdf=render endpoint renders the same open-access article and
    has no challenge.
  * ahajournals.org / journals.lww.com / academic.oup.com sit behind bot
    challenges that a header set does not defeat; those are manual-download.
  * web.archive.org intermittently returns 503 -- retry.
  * Some sources are HTML-only (CDC landing pages). Those are rendered to PDF
    with headless Chrome so the docling ingestion path (which only accepts
    .pdf) can consume them.

Usage:
    uv run scripts/fetch_guideline_pdfs.py                 # fetch everything
    uv run scripts/fetch_guideline_pdfs.py --only critical_care
    uv run scripts/fetch_guideline_pdfs.py --source KDIGO_AKI_2012
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PDF_DIR = REPO / "guidelines" / "pdfs"
SOURCES = REPO / "guidelines" / "sources.json"
LEDGER = PDF_DIR / "_ledger.json"



def _find_chrome() -> str | None:
    """Locate a headless-capable Chrome/Chromium: $CHROME, then PATH, then the macOS bundle."""
    env = os.environ.get("CHROME")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if Path(mac).exists() else None


CHROME = _find_chrome()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = [
    f"User-Agent: {UA}",
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,application/pdf,*/*;q=0.8",
    "Accept-Language: en-US,en;q=0.9",
    'sec-ch-ua: "Chromium";v="126", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile: ?0",
    'sec-ch-ua-platform: "macOS"',
    "Sec-Fetch-Dest: document",
    "Sec-Fetch-Mode: navigate",
    "Sec-Fetch-Site: none",
    "Upgrade-Insecure-Requests: 1",
]

# route kinds:
#   pdf     -- direct PDF download over curl with browser headers
#   epmc    -- Europe PMC ?pdf=render for an open-access PMCID
#   wayback -- Wayback Machine capture of a PDF (id_ raw form)
#   html2pdf-- render an HTML page to PDF with headless Chrome
#   manual  -- known bot-walled / paywalled; human must download
ROUTES: dict[str, list[tuple[str, str]]] = {
    # ---- critical care (the 11 that gate the baseline runs) ----
    "SCCM_ESICM_Surviving_Sepsis_2026": [
        ("pdf", "https://link.springer.com/content/pdf/10.1007/s00134-026-08361-1.pdf"),
        ("manual", "journals.lww.com JS bot challenge; not in Europe PMC (PMID 41869847)"),
    ],
    "BTF_Severe_TBI_4th_Edition_2017": [
        ("pdf", "https://braintrauma.org/s/Management_of_Severe_TBI_4th_Edition.pdf"),
    ],
    "NCS_Status_Epilepticus_2012": [
        ("pdf", "https://link.springer.com/content/pdf/10.1007/s12028-012-9695-z.pdf"),
        ("manual", "Springer paywalled; PMID 22528274"),
    ],
    "AHA_ASA_Spontaneous_ICH_2022": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/STR.0000000000000407"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/STR.0000000000000407"),
        ("manual", "ahajournals.org bot challenge; free to read in a browser"),
    ],
    "KDIGO_AKI_2012": [
        (
            "pdf",
            "https://kdigo.org/wp-content/uploads/2016/10/" "KDIGO-2012-AKI-Guideline-English.pdf",
        ),
    ],
    "ATS_IDSA_CAP_2019": [
        ("epmc", "PMC6812437"),
    ],
    "IDSA_ATS_HAP_VAP_2016": [
        ("epmc", "PMC4981759"),
    ],
    "SCCM_PADIS_2018": [
        (
            "wayback",
            "https://journals.lww.com/ccmjournal/fulltext/2018/09000/"
            "clinical_practice_guidelines_for_the_prevention.29.aspx",
        ),
        ("manual", "journals.lww.com paywalled; PMID 30113379"),
    ],
    "NHLBI_ARDSNet_Ventilator_Protocol": [
        # HTTPS is intentionally not used: the host's TLS cert does not match.
        ("pdf", "http://www.ardsnet.org/files/ventilator_protocol_2008-07.pdf"),
    ],
    "CDC_Core_Elements_Hospital_Antibiotic_Stewardship": [
        ("pdf", "https://www.cdc.gov/antibiotic-use/media/pdfs/core-elements-hospital.pdf"),
        (
            "pdf",
            "https://www.cdc.gov/antibiotic-use/core-elements/pdfs/hospital-core-elements-H.pdf",
        ),
        ("html2pdf", "https://www.cdc.gov/antibiotic-use/hcp/core-elements/hospital.html"),
    ],
    "CDC_NHSN_PSC_Surveillance_Definitions": [
        ("pdf", "https://www.cdc.gov/nhsn/pdfs/pscmanual/pscmanual_current.pdf"),
    ],
    # ---- infection control / ID ----
    "CDC_HICPAC_Healthcare_Pneumonia_2003": [
        ("pdf", "https://www.cdc.gov/mmwr/pdf/rr/rr5303.pdf"),
        ("wayback", "https://www.cdc.gov/mmwr/PDF/rr/rr5303.pdf"),
    ],
    "CDC_HICPAC_NICU_CLABSI": [
        ("html2pdf", "https://www.cdc.gov/infection-control/hcp/nicu-clabsi/index.html"),
    ],
    "CDC_Outpatient_Oncology_Infection_Control_2011": [
        (
            "pdf",
            "https://www.cdc.gov/healthcare-associated-infections/media/pdfs/"
            "basic-infection-control-prevention-plan-2011-508.pdf",
        ),
    ],
    "CDC_Influenza_Treatment": [
        ("html2pdf", "https://www.cdc.gov/flu/hcp/antivirals/index.html"),
        ("html2pdf", "https://www.cdc.gov/flu/treatment/index.html"),
    ],
    # ---- chronic disease / cardiology ----
    "ADA_Standards_of_Care_2026": [
        ("pdf", "https://diabetesjournals.org/care/article-pdf/49/Supplement_1/S1/"),
        ("manual", "diabetesjournals.org bot challenge; free to read in a browser"),
    ],
    "AHA_ACC_High_Blood_Pressure_2025": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001356"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001356"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    "ACC_AHA_Acute_Coronary_Syndromes_2025": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001309"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001309"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    "AHA_ACC_HFSA_Heart_Failure_2022": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001063"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001063"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    "AHA_ACC_Chest_Pain_2021": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001029"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001029"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    "AHA_ACC_Chronic_Coronary_Disease_2023": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001168"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001168"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    "ACC_AHA_Peripheral_Artery_Disease_2024": [
        ("epmc", "PMC12782132"),
    ],
    # Added 2026-08-21 (v3 corpus expansion): the v2 corpus had no source
    # covering anticoagulation indication / INR management or ascites-SBP workup.
    "ACC_AHA_ACCP_HRS_Atrial_Fibrillation_2023": [
        ("epmc", "PMC11095842"),
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001193"),
        ("wayback", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001193"),
        ("manual", "ahajournals.org bot challenge; free to read in a browser"),
    ],
    "AASLD_Ascites_Cirrhosis_2021": [
        ("pdf", "https://www.aasld.org/sites/default/files/2021-08/AASLD-Ascites-2021.pdf"),
        ("wayback", "https://aasldpubs.onlinelibrary.wiley.com/doi/pdf/10.1002/hep.31884"),
        ("manual", "Wiley bot challenge; PMID 33942342"),
    ],
    # NICE resource PDFs are served to plain clients without a challenge, so they
    # are the retrievable substitutes for the Cloudflare-walled society guidelines
    # above (see sources.json coverage_note).
    "NICE_Atrial_Fibrillation_NG196_2021": [
        (
            "pdf",
            "https://www.nice.org.uk/guidance/ng196/resources/"
            "atrial-fibrillation-diagnosis-and-management-pdf-66142085507269",
        ),
    ],
    "NICE_Chronic_Heart_Failure_NG106_2018": [
        (
            "pdf",
            "https://www.nice.org.uk/guidance/ng106/resources/"
            "chronic-heart-failure-in-adults-diagnosis-and-management-pdf-66141541311685",
        ),
    ],
    "NICE_Hypertension_NG136_2019": [
        (
            "pdf",
            "https://www.nice.org.uk/guidance/ng136/resources/"
            "hypertension-in-adults-diagnosis-and-management-pdf-66141722710213",
        ),
    ],
    "NICE_Type_2_Diabetes_NG28_2022": [
        (
            "pdf",
            "https://www.nice.org.uk/guidance/ng28/resources/"
            "type-2-diabetes-in-adults-management-pdf-1837338615493",
        ),
    ],
    "NICE_Blood_Transfusion_NG24_2015": [
        (
            "pdf",
            "https://www.nice.org.uk/guidance/ng24/resources/"
            "blood-transfusion-pdf-1837331897029",
        ),
    ],
    "EASL_Decompensated_Cirrhosis_2018": [
        (
            "pdf",
            "https://easl.eu/wp-content/uploads/2018/10/"
            "decompensated-cirrhosis-English-report.pdf",
        ),
    ],
    "ESUR_Contrast_Agents_2018": [
        (
            "pdf",
            "https://www.esur.org/wp-content/uploads/2022/03/"
            "ESUR-Guidelines-10_0-Final-Version.pdf",
        ),
    ],
    "JBDS_Steroid_Hyperglycaemia_2023": [
        (
            "pdf",
            "https://abcd.care/sites/default/files/site_uploads/JBDS_Guidelines_Current/"
            "JBDS_08_Management_of_Hyperglycaemia_and_Steroid_%28Glucocorticoid%29_"
            "Therapy_with_QR_code_January_2023.pdf",
        ),
    ],
    "ACC_AHA_Adult_Congenital_Heart_Disease_2025": [
        ("pdf", "https://www.ahajournals.org/doi/pdf/10.1161/CIR.0000000000001402"),
        ("manual", "ahajournals.org bot challenge"),
    ],
    # ---- other specialties ----
    "NHLBI_NAEPP_Asthma_EPR3_2007": [
        ("pdf", "https://www.nhlbi.nih.gov/sites/default/files/media/docs/asthgdln_1.pdf"),
        ("pdf", "https://www.nhlbi.nih.gov/files/docs/guidelines/asthgdln.pdf"),
        ("wayback", "https://www.nhlbi.nih.gov/files/docs/guidelines/asthgdln.pdf"),
    ],
    "NIAID_Food_Allergy_Guidelines_2010": [
        (
            "pdf",
            "https://www.niaid.nih.gov/sites/default/files/" "FoodAllergyCPGReport.pdf",
        ),
        ("wayback", "https://www.niaid.nih.gov/sites/default/files/faguidelinesexecsummary.pdf"),
        (
            "html2pdf",
            "https://www.niaid.nih.gov/diseases-conditions/"
            "guidelines-clinicians-and-patients-food-allergy",
        ),
    ],
    "AASM_Insomnia_Psych_Behavioral_Treatment_2006": [
        ("pdf", "https://academic.oup.com/sleep/article-pdf/29/11/1415/"),
        ("manual", "academic.oup.com bot challenge; aasm.org PDF path returns 502"),
    ],
    "AAFP_Acute_Migraine_Management_2002": [
        ("html2pdf", "https://www.aafp.org/pubs/afp/issues/2018/0215/p243.html"),
        ("html2pdf", "https://www.aafp.org/pubs/afp/issues/2002/1201/p2123.html"),
    ],
    "AAO_HNSF_Allergic_Rhinitis_2015": [
        ("pdf", "https://onlinelibrary.wiley.com/doi/pdf/10.1177/0194599814561600"),
        ("manual", "publisher migrated SAGE -> Wiley; both bot-walled"),
    ],
    "AAP_Mind_Body_Therapies_2016": [
        ("pdf", "https://publications.aap.org/pediatrics/article-pdf/138/3/e20161896/"),
        ("manual", "publications.aap.org bot challenge"),
    ],
}


def _curl(url: str, dest: Path, timeout: int = 180) -> tuple[bool, str]:
    """Download url to dest with a full browser header set. Returns (ok, detail)."""
    cmd = [
        "curl",
        "-sSL",
        "--compressed",
        "--max-time",
        str(timeout),
        "-o",
        str(dest),
        "-w",
        "%{http_code} %{content_type} %{size_download}",
    ]
    for h in BROWSER_HEADERS:
        cmd += ["-H", h]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    detail = proc.stdout.strip() or proc.stderr.strip()
    return _is_pdf(dest), detail


def _is_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 4096:
        return False
    with open(path, "rb") as fh:
        return fh.read(5) == b"%PDF-"


def _wayback_snapshot(url: str) -> str | None:
    """Return the raw (id_) Wayback URL for the closest capture, if any."""
    api = f"https://archive.org/wayback/available?url={url}"
    proc = subprocess.run(["curl", "-sS", "--max-time", "60", api], capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    closest = payload.get("archived_snapshots", {}).get("closest")
    if not closest or not closest.get("available"):
        return None
    # if_ / id_ suffix returns the raw archived bytes instead of the Wayback chrome
    return (
        closest["url"].replace("/http", "if_/http", 1)
        if "if_/" not in closest["url"]
        else closest["url"]
    )


def _html_to_pdf(url: str, dest: Path) -> tuple[bool, str]:
    """Render an HTML page to PDF with headless Chrome."""
    if not CHROME:
        return False, "headless Chrome not installed (set $CHROME or put google-chrome/chromium on PATH)"
    proc = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--virtual-time-budget=15000",
            "--run-all-compositor-stages-before-draw",
            f"--print-to-pdf={dest}",
            "--no-pdf-header-footer",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    ok = _is_pdf(dest)
    return ok, f"chrome rc={proc.returncode} size={dest.stat().st_size if dest.exists() else 0}"


def fetch_one(source: str, routes: list[tuple[str, str]]) -> dict:
    dest = PDF_DIR / f"{source}.pdf"
    if _is_pdf(dest):
        return {
            "source": source,
            "status": "fetched",
            "route": "already_present",
            "bytes": dest.stat().st_size,
        }

    attempts: list[dict] = []
    for kind, target in routes:
        if kind == "manual":
            attempts.append({"route": "manual", "detail": target})
            break

        if kind == "pdf":
            ok, detail = _curl(target, dest)
        elif kind == "epmc":
            ok, detail = _curl(f"https://europepmc.org/articles/{target}?pdf=render", dest)
        elif kind == "wayback":
            snap = _wayback_snapshot(target)
            if snap is None:
                ok, detail = False, "no wayback capture"
            else:
                ok, detail = False, ""
                # wayback 503s intermittently; retry a few times
                for _ in range(4):
                    ok, detail = _curl(snap, dest)
                    if ok:
                        break
                    time.sleep(8)
        elif kind == "html2pdf":
            ok, detail = _html_to_pdf(target, dest)
        else:  # pragma: no cover - guarded by ROUTES contents
            ok, detail = False, f"unknown route kind {kind}"

        attempts.append({"route": kind, "target": target, "ok": ok, "detail": detail})
        if ok:
            return {
                "source": source,
                "status": "fetched",
                "route": kind,
                "target": target,
                "bytes": dest.stat().st_size,
                "attempts": attempts,
            }

    if dest.exists() and not _is_pdf(dest):
        dest.unlink()
    return {"source": source, "status": "manual_download_needed", "attempts": attempts}


def select_targets(categories: dict[str, str], only: str | None, source: str | None) -> list[str]:
    """Routable sources, narrowed to one category and/or one source key when asked."""
    targets = [s for s in categories if s in ROUTES]
    if only:
        targets = [s for s in targets if categories[s] == only]
    if source:
        targets = [s for s in targets if s == source]
    return targets


def fetch_all(targets: list[str], categories: dict[str, str]) -> list[dict]:
    results = []
    for source in targets:
        print(f"--- {source} ({categories[source]})", flush=True)
        res = fetch_one(source, ROUTES[source])
        res["category"] = categories[source]
        print(f"    -> {res['status']} via {res.get('route', '-')}", flush=True)
        results.append(res)
    return results


def merge_ledger(results: list[dict], partial_run: bool) -> dict:
    """New ledger; a partial run keeps prior entries for sources it did not touch."""
    ledger = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": results}
    if LEDGER.exists() and partial_run:
        prior = json.loads(LEDGER.read_text()).get("results", [])
        refreshed = {r["source"] for r in results}
        keep = [r for r in prior if r["source"] not in refreshed]
        ledger["results"] = keep + results
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="restrict to one sources.json category")
    parser.add_argument("--source", help="restrict to one source key")
    args = parser.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(SOURCES.read_text())
    categories = {g["source"]: g["category"] for g in manifest["guidelines"]}

    targets = select_targets(categories, args.only, args.source)
    results = fetch_all(targets, categories)

    ledger = merge_ledger(results, partial_run=bool(args.only or args.source))
    LEDGER.write_text(json.dumps(ledger, indent=2))

    fetched = [r for r in ledger["results"] if r["status"] == "fetched"]
    cc = [r for r in fetched if r["category"] == "critical_care"]
    print(
        f"\nfetched {len(fetched)}/{len(ledger['results'])} "
        f"({len(cc)} critical_care) -- ledger: {LEDGER}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
