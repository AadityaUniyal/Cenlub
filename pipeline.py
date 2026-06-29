"""
pipeline.py  —  Unified Cenlub RFQ → Annexure-I BOM CSV Pipeline
=================================================================
Phases (from project_pipeline_plan.txt):

  Phase 0  Setup & schema definition
  Phase 1  Email intake & metadata extraction
  Phase 2  PDF parsing (O&M spares, LOS spec, Technical Part, Mandatory Spares)
           2A  Spares table with category codes A/B/C/D + applicability flags
           2B  LOS parameter extraction → checklist
           2C  Technical specification → MOC per item
           2D  Mandatory spares cross-reference
  Phase 3  BOM consolidation
           3.1  Merge & deduplicate (QuadraticHashTable)
           3.2  Internal item code assignment (L/M/I/E/P prefix)
           3.3  Assembly group assignment
           3.4  Vendor inquiry package generation
  Phase 4  CSV generation (two files)
           4.1  Annexure-I with header block (BOM_*.csv)
           4.7  Customer-facing spares summary (SPARES_SUMMARY_*.csv)
  Phase 5  Checklist export (CHECKLIST_*.csv)
           5.3  Missing document alert email
  Phase 6  Vendor pricing ingestion & priced BOM export

Usage:
    python pipeline.py
    python pipeline.py --input data/raw --output-dir data/processed
    python pipeline.py --gemini-key YOUR_KEY
    python pipeline.py --vendor-file path/to/filled_inquiry.xlsx
    python pipeline.py --help
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from src.core.models import (
    BOMNode, ChecklistDoc, EmailMetadata,
    MandatorySpare, OMSpare, QuadraticHashTable, RMCData,
)
from src.core.validators import validate_rmc
from src.extraction.bom_extractor import clean_price, extract_bom, split_tags
from src.extraction.classifier import classify
from src.extraction.rmc_extractor import (
    assign_assembly_groups,
    assign_item_codes,
    cross_reference_mandatory,
    extract_email_metadata,
    extract_fields,
    match_technical_specs,
    merge_extractions,
    parse_spares_table_full,
    split_email_thread,
)
from src.ingestion.loader import load_pdf
from src.llm.gemini_client import (
    auditor_agent,
    extractor_agent,
    fill_checklist_from_text,
    generate_plain_descriptions,
)
from src.output.csv_writer import (
    write_annexure_csv,
    write_checklist_csv,
    write_spares_summary_csv,
)
from src.output.excel_writer import write_excel
from src.output.missing_doc_alert import send_missing_doc_alert
from src.output.vendor_inquiry import (
    send_vendor_inquiry,
    write_vendor_inquiry_csv,
    write_vendor_inquiry_xlsx,
)
from src.output.vendor_pricing import (
    apply_vendor_pricing,
    finalize_bom_pricing,
    ingest_vendor_pricing,
    write_priced_annexure,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")

_RMC_FIELDS = [
    "lead_no", "project_name", "customer", "application", "industry",
    "flow_rate", "pressure", "temperature", "oil_grade", "filtration_level",
    "heat_exchanger_moc", "reservoir_moc", "control_panel_requirement",
]


# ── Lead number helper (Phase 1.3) ────────────────────────────────────────────

def _make_lead_no() -> str:
    """Auto-generate a Lead No. in format P[YY][MM][NNNNN]."""
    today = date.today()
    counter_path = os.path.join("data", ".lead_counter")
    os.makedirs("data", exist_ok=True)
    try:
        counter = int(open(counter_path).read().strip()) + 1
    except Exception:
        counter = 1
    with open(counter_path, "w") as f:
        f.write(str(counter))
    return f"P{today.strftime('%y%m')}{counter:05d}"


# ── Output filename builder (Phase 4.6) ───────────────────────────────────────

def _build_filename(lead: str, rfq: str, project: str, customer: str,
                    recv_date: str, prefix: str, ext: str) -> str:
    def _slug(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "", s.upper())[:12]

    parts = [prefix, lead, _slug(rfq), _slug(project), _slug(customer)]
    date_slug = re.sub(r"[^A-Za-z0-9]", "", recv_date)[:9]
    if date_slug:
        parts.append(date_slug)
    return "_".join(p for p in parts if p) + ext


# ── Main pipeline run function ────────────────────────────────────────────────

def run(
    input_dir: str = "data/raw",
    output_dir: str = "data/processed",
    gemini_key: str = "",
    groq_key: str = "",
    vendor_file: str = "",
    vendor_emails: Optional[List[str]] = None,
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_user: str = "",
    smtp_pass: str = "",
    thresholds: Optional[Dict] = None,
) -> dict:
    """
    Full Cenlub RFQ → Annexure-I BOM pipeline.

    Returns a result dict with file paths and validation summary.
    """
    os.makedirs(output_dir, exist_ok=True)

    # ── Phase 0: resolve API keys from env if not passed ─────────────────────
    gemini_key = gemini_key or os.getenv("GEMINI_API_KEY", "")
    groq_key = groq_key or os.getenv("GROQ_API_KEY", "")
    smtp_host = smtp_host or os.getenv("SMTP_HOST", "")
    smtp_port = smtp_port or int(os.getenv("SMTP_PORT", "587"))
    smtp_user = smtp_user or os.getenv("SMTP_USER", "")
    smtp_pass = smtp_pass or os.getenv("SMTP_PASS", "")
    thresholds = thresholds or {}

    # ── Phase 1: ingest all PDFs in input_dir ────────────────────────────────
    pdf_files = [
        os.path.join(input_dir, fn)
        for fn in sorted(os.listdir(input_dir))
        if fn.lower().endswith(".pdf")
    ]
    if not pdf_files:
        log.warning("No PDF files found in %s. Exiting.", input_dir)
        return {"status": "no_input"}

    log.info("Phase 1: Ingesting %d PDF(s) from %s", len(pdf_files), input_dir)
    docs: List[dict] = []
    for path in pdf_files:
        doc = load_pdf(path)
        doc["classification"] = classify(doc["full_text"])
        log.info("  %-40s → %s", doc["pdf_name"], doc["classification"])
        docs.append(doc)

    # ── Phase 1.2: Email metadata extraction ─────────────────────────────────
    meta: Optional[EmailMetadata] = None
    for doc in docs:
        if doc["classification"] == "Email":
            meta_dict = extract_email_metadata(doc["full_text"])
            meta = EmailMetadata(**meta_dict)
            log.info(
                "Phase 1.2: Email metadata — RFQ: %s | Project: %s | Customer: %s",
                meta.customer_rfq_ref, meta.project_name, meta.customer_name,
            )
            break

    # ── Phase 1.3: Lead number assignment ────────────────────────────────────
    if meta and meta.lead_no == "XX":
        meta.lead_no = _make_lead_no()
        log.info("Phase 1.3: Assigned Lead No. %s", meta.lead_no)

    # Shorthand references
    lead_no = (meta.lead_no if meta and meta.lead_no != "XX" else "")
    rfq_ref = (meta.customer_rfq_ref if meta and meta.customer_rfq_ref != "XX" else "")
    project = (meta.project_name if meta and meta.project_name != "XX" else "")
    customer_n = (meta.customer_name if meta and meta.customer_name != "XX" else "")
    date_rcv = (meta.date_received if meta and meta.date_received != "XX" else "")
    contact = (meta.contact_person if meta else "")
    contact_em = (meta.contact_email if meta else "")

    # ── Phase 2: rule-based extraction across all docs ────────────────────────
    log.info("Phase 2: Extracting fields from %d documents…", len(docs))
    all_extractions: List[dict] = []
    combined_texts: List[str] = []

    for doc in docs:
        cls = doc["classification"]
        text = doc["full_text"]
        combined_texts.append(text)

        if cls == "Email":
            for seg in reversed(split_email_thread(text)):
                all_extractions.append(
                    {"classification": "Email", "fields": extract_fields(seg)}
                )
        else:
            all_extractions.append(
                {"classification": cls, "fields": extract_fields(text)}
            )

    merged = merge_extractions(all_extractions)
    combined_text = "\n\n".join(combined_texts)

    # ── Phase 2: LLM extractor + auditor agents ───────────────────────────────
    if gemini_key:
        log.info("Phase 2: Running Gemini extractor agent…")
        llm = extractor_agent(combined_text, merged, gemini_key)
        log.info("Phase 2: Running Gemini auditor agent…")
        llm = auditor_agent(combined_text, llm, gemini_key)
        for key in _RMC_FIELDS:
            if merged.get(key, "XX") == "XX" and llm.get(key, "XX") != "XX":
                merged[key] = llm[key]
        mand_list = merged.get("mandatory_spares") or llm.get("mandatory_spares", [])
        om_list = merged.get("om_spares") or llm.get("om_spares", [])
    else:
        mand_list = merged.get("mandatory_spares", [])
        om_list = merged.get("om_spares", [])

    def _to_spare(raw, cls_):
        try:
            return cls_(**raw) if isinstance(raw, dict) else raw
        except Exception:
            return None

    rmc = RMCData(**{
        **{k: merged.get(k, "XX") for k in _RMC_FIELDS},
        "mandatory_spares": [
            s for s in (_to_spare(r, MandatorySpare) for r in mand_list) if s
        ],
        "om_spares": [
            s for s in (_to_spare(r, OMSpare) for r in om_list) if s
        ],
    })

    # Backfill lead_no into rmc if extracted from email
    if rmc.lead_no == "XX" and lead_no:
        rmc = rmc.model_copy(update={"lead_no": lead_no})
    elif not lead_no and rmc.lead_no != "XX":
        lead_no = rmc.lead_no

    log.info(
        "Phase 2: RMC extracted — Lead: %s | Flow: %s | Pressure: %s",
        rmc.lead_no, rmc.flow_rate, rmc.pressure,
    )

    # ── Phase 2A: Full spares table parsing (category codes + flags) ──────────
    log.info("Phase 2A: Parsing spares tables with category codes…")
    bom_rows: List[dict] = []
    mand_rows: List[dict] = []

    for doc in docs:
        cls = doc["classification"]
        for tbl in doc.get("tables", []):
            data = tbl["data"]
            if not data:
                continue
            if cls == "O&M Spares":
                bom_rows.extend(parse_spares_table_full(
                    data, doc["pdf_name"],
                    lead_no=lead_no, rfq_ref=rfq_ref,
                    project=project, customer=customer_n, date=date_rcv,
                ))
            elif cls == "Mandatory Spares":
                mand_rows.extend(parse_spares_table_full(
                    data, doc["pdf_name"],
                    lead_no=lead_no, rfq_ref=rfq_ref,
                    project=project, customer=customer_n, date=date_rcv,
                ))

    log.info(
        "Phase 2A: %d O&M rows, %d mandatory rows extracted.",
        len(bom_rows), len(mand_rows),
    )

    # ── Phase 2D: Mandatory cross-reference ──────────────────────────────────
    if mand_rows:
        log.info("Phase 2D: Cross-referencing mandatory spares…")
        bom_rows = cross_reference_mandatory(bom_rows, mand_rows)

    # ── Phase 2C: Technical spec → MOC per item ───────────────────────────────
    tech_texts = [
        d["full_text"] for d in docs
        if d["classification"] == "Technical Specification"
    ]
    if tech_texts and bom_rows:
        log.info("Phase 2C: Matching technical specifications to BOM items…")
        bom_rows = match_technical_specs(bom_rows, "\n\n".join(tech_texts))

    # ── Phase 2A.3: LLM plain descriptions ───────────────────────────────────
    if gemini_key and bom_rows:
        log.info("Phase 2A.3: Generating plain descriptions via Gemini…")
        bom_rows = generate_plain_descriptions(bom_rows, gemini_key)

    # ── Phase 3.2 & 3.3: Item codes and assembly groups ──────────────────────
    log.info("Phase 3.2: Assigning internal item codes…")
    bom_rows = assign_item_codes(bom_rows)
    log.info("Phase 3.3: Assigning assembly groups…")
    bom_rows = assign_assembly_groups(bom_rows)

    # ── Phase 3.1: BOM equipment tree (dedup via QuadraticHashTable) ──────────
    log.info("Phase 3.1: Building BOM equipment tree…")
    bom_root = BOMNode("root", "root")
    seen = QuadraticHashTable()
    for doc in docs:
        bom_struct = extract_bom(doc, groq_key, gemini_key)
        if not bom_struct:
            continue
        for eq in bom_struct.equipment_list:
            tags = split_tags(eq.equipment_tag) if eq.equipment_tag else [None]
            for tag in tags:
                sig = f"{eq.description.strip().lower()}_{tag or ''}"
                if not seen.add(sig):
                    continue
                eq_node = BOMNode(eq.description, "equipment", {
                    "item_no": eq.item_no,
                    "qty": eq.qty,
                    "material_specification": eq.material_specification or "",
                    "unit_price": clean_price(eq.unit_price or ""),
                    "equipment_tag": tag or eq.equipment_tag or "",
                    "source_file": doc["pdf_name"],
                })
                for comp in eq.components:
                    eq_node.add_child(BOMNode(
                        comp.name, "component",
                        {"material": comp.material or "", "qty": comp.qty or ""},
                    ))
                bom_root.add_child(eq_node)

    log.info(
        "Phase 3.1: BOM tree has %d equipment nodes.", len(bom_root.children)
    )

    # ── Phase 4.5: Validation ─────────────────────────────────────────────────
    log.info("Phase 4.5: Running validation and sanity checks…")
    validation = validate_rmc(rmc, thresholds)
    if validation["warnings"]:
        for w in validation["warnings"]:
            log.warning("  %s", w)
    log.info(
        "Phase 4.5: Confidence score: %.1f%%  |  %d warnings",
        validation["confidence_score"], len(validation["warnings"]),
    )

    # ── Phase 5: Checklist auto-fill ──────────────────────────────────────────
    log.info("Phase 5: Filling checklist…")
    checklist_dict: dict = {}
    if gemini_key:
        checklist_dict = fill_checklist_from_text(combined_text, gemini_key)

    cls_names = [d["classification"] for d in docs]
    checklist_dict.setdefault(
        "customer_ts_los",
        "RECEIVED" if "LOS Specification" in cls_names else "MISSING",
    )
    checklist_dict.setdefault(
        "mech_spec",
        "RECEIVED" if "Technical Specification" in cls_names else "MISSING",
    )
    checklist_dict.setdefault(
        "other_docs",
        "RECEIVED" if any(
            c in cls_names for c in ("O&M Spares", "Mandatory Spares")
        ) else "MISSING",
    )
    checklist_dict["lead_no"] = lead_no or "XX"
    checklist_dict["date"] = date_rcv or date.today().strftime("%d-%b-%Y")
    checklist_dict["prepared_by"] = "ASHISH MON"

    checklist = ChecklistDoc(**{
        k: v for k, v in checklist_dict.items()
        if k in ChecklistDoc.model_fields
    })

    # ── Phase 4.6: BOM CSV (Annexure-I) ──────────────────────────────────────
    log.info("Phase 4.6: Writing Annexure-I BOM CSV…")
    bom_csv = os.path.join(
        output_dir,
        _build_filename(lead_no, rfq_ref, project, customer_n, date_rcv, "BOM", ".csv"),
    )
    write_annexure_csv(
        rmc, bom_rows, bom_root, validation["warnings"], bom_csv, meta,
    )
    log.info("  → %s", bom_csv)

    # ── Phase 4.7: Customer-facing spares summary ─────────────────────────────
    log.info("Phase 4.7: Writing spares summary CSV…")
    spares_csv = os.path.join(
        output_dir,
        _build_filename(lead_no, rfq_ref, "", "", "", "SPARES_SUMMARY", ".csv"),
    )
    write_spares_summary_csv(bom_rows, rmc, spares_csv, meta)
    log.info("  → %s", spares_csv)

    # ── Phase 5.4: Checklist CSV ──────────────────────────────────────────────
    log.info("Phase 5.4: Writing checklist CSV…")
    checklist_csv = os.path.join(
        output_dir,
        _build_filename(
            lead_no, rfq_ref, "", "", date_rcv, "CHECKLIST", ".csv"
        ),
    )
    write_checklist_csv(checklist, checklist_csv)
    log.info("  → %s", checklist_csv)

    # ── Excel workbook ────────────────────────────────────────────────────────
    log.info("Writing Excel workbook…")
    excel_path = os.path.join(
        output_dir,
        _build_filename(lead_no, rfq_ref, project, customer_n, date_rcv, "RMC", ".xlsx"),
    )
    write_excel(rmc, excel_path)
    log.info("  → %s", excel_path)

    # ── Phase 3.4: Vendor inquiry package ────────────────────────────────────
    log.info("Phase 3.4: Generating vendor inquiry package…")
    vendor_csv_path = os.path.join(
        output_dir,
        _build_filename(
            lead_no, rfq_ref, project, customer_n, date_rcv, "VENDOR_INQUIRY", ".csv"
        ),
    )
    vendor_xlsx_path = vendor_csv_path.replace(".csv", ".xlsx")

    write_vendor_inquiry_csv(
        bom_rows, lead_no, rfq_ref, project, customer_n, vendor_csv_path,
    )
    write_vendor_inquiry_xlsx(
        bom_rows, lead_no, rfq_ref, project, customer_n, vendor_xlsx_path,
    )
    log.info("  → %s", vendor_csv_path)
    log.info("  → %s", vendor_xlsx_path)

    # Optionally email the vendor inquiry
    if vendor_emails:
        log.info("Phase 3.4: Sending vendor inquiry to %d address(es)…", len(vendor_emails))
        send_result = send_vendor_inquiry(
            vendor_emails=vendor_emails,
            lead_no=lead_no,
            rfq_ref=rfq_ref,
            project=project,
            attachment_paths=[vendor_xlsx_path],
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_pass=smtp_pass,
        )
        log.info(
            "Phase 3.4: Email result — sent: %s | failed: %s | dry_run: %s",
            send_result.get("sent"),
            send_result.get("failed"),
            send_result.get("dry_run"),
        )
    else:
        send_result = {"dry_run": True, "note": "No vendor emails provided"}

    # ── Phase 5.3: Missing document alert ────────────────────────────────────
    log.info("Phase 5.3: Checking for missing critical documents…")
    alert_result = send_missing_doc_alert(
        checklist=checklist,
        contact_name=contact,
        contact_email=contact_em,
        lead_no=lead_no,
        rfq_ref=rfq_ref,
        project=project,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
    )
    if alert_result["status"] == "nothing_missing":
        log.info("Phase 5.3: All critical documents present.")
    elif alert_result["status"] == "dry_run":
        log.warning(
            "Phase 5.3: Missing docs detected — %s. "
            "Alert not sent (SMTP not configured).",
            alert_result.get("missing"),
        )
    elif alert_result["status"] == "sent":
        log.info(
            "Phase 5.3: Missing doc alert sent to %s.", contact_em
        )

    # ── Phase 6: Vendor pricing ingestion (optional) ──────────────────────────
    priced_bom_csv = ""
    if vendor_file and os.path.exists(vendor_file):
        log.info("Phase 6: Ingesting vendor pricing from %s…", vendor_file)
        vendor_rows = ingest_vendor_pricing(vendor_file)
        bom_rows = apply_vendor_pricing(bom_rows, vendor_rows)
        bom_rows = finalize_bom_pricing(bom_rows)

        priced_result = write_priced_annexure(
            rmc=rmc,
            bom_rows=bom_rows,
            bom_root=bom_root,
            warnings=validation["warnings"],
            out_dir=output_dir,
            lead_no=lead_no,
            rfq_ref=rfq_ref,
            project=project,
            customer=customer_n,
            date_rcv=date_rcv,
            meta=meta,
        )
        priced_bom_csv = priced_result["bom_csv"]
        log.info("Phase 6: Priced BOM → %s", priced_bom_csv)
    else:
        log.info(
            "Phase 6: No vendor pricing file provided — "
            "BOM CSV left with blank price/make/delivery columns."
        )

    # ── Summary JSON ──────────────────────────────────────────────────────────
    summary = {
        "lead_no": lead_no or rmc.lead_no,
        "rfq_ref": rfq_ref,
        "project": project or rmc.project_name,
        "customer": customer_n or rmc.customer,
        "date_received": date_rcv,
        "bom_rows_total": len(bom_rows),
        "bom_rows_applicable": sum(
            1 for r in bom_rows if r.get("applicable_flag", True)
        ),
        "bom_rows_mandatory": sum(
            1 for r in bom_rows if r.get("mandatory_flag", False)
        ),
        "validation": validation,
        "output_files": {
            "bom_csv": bom_csv,
            "spares_csv": spares_csv,
            "checklist_csv": checklist_csv,
            "excel": excel_path,
            "vendor_inquiry_csv": vendor_csv_path,
            "vendor_inquiry_xlsx": vendor_xlsx_path,
            "priced_bom_csv": priced_bom_csv,
        },
        "vendor_email": send_result,
        "missing_doc_alert": alert_result,
    }

    summary_path = os.path.join(output_dir, "pipeline_summary.json")
    with open(summary_path, "w", encoding="utf-8") as jf:
        json.dump(summary, jf, indent=2, default=str)
    log.info("Pipeline complete. Summary → %s", summary_path)

    return summary


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cenlub RFQ → Annexure-I BOM Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input", default="data/raw",
        help="Directory containing input PDF files",
    )
    p.add_argument(
        "--output-dir", default="data/processed",
        help="Directory for all output files",
    )
    p.add_argument(
        "--gemini-key", default="",
        help="Google Gemini API key (or set GEMINI_API_KEY env var)",
    )
    p.add_argument(
        "--groq-key", default="",
        help="Groq API key for BOM extraction (or set GROQ_API_KEY env var)",
    )
    p.add_argument(
        "--vendor-file", default="",
        help="Path to vendor-filled inquiry CSV/XLSX for Phase 6 pricing ingestion",
    )
    p.add_argument(
        "--vendor-emails", default="",
        help="Comma-separated vendor email addresses for Phase 3.4 inquiry email",
    )
    p.add_argument(
        "--smtp-host", default="",
        help="SMTP server hostname for email sending",
    )
    p.add_argument(
        "--smtp-port", type=int, default=587,
        help="SMTP server port",
    )
    p.add_argument(
        "--smtp-user", default="",
        help="SMTP login username / from address",
    )
    p.add_argument(
        "--smtp-pass", default="",
        help="SMTP login password",
    )
    p.add_argument(
        "--flow-min", type=float, default=5.0,
        help="Sanity check: minimum flow rate (LPM)",
    )
    p.add_argument(
        "--flow-max", type=float, default=2000.0,
        help="Sanity check: maximum flow rate (LPM)",
    )
    p.add_argument(
        "--press-min", type=float, default=1.0,
        help="Sanity check: minimum pressure (Bar)",
    )
    p.add_argument(
        "--press-max", type=float, default=50.0,
        help="Sanity check: maximum pressure (Bar)",
    )
    p.add_argument(
        "--temp-min", type=float, default=10.0,
        help="Sanity check: minimum temperature (Deg C)",
    )
    p.add_argument(
        "--temp-max", type=float, default=100.0,
        help="Sanity check: maximum temperature (Deg C)",
    )
    p.add_argument(
        "--filt-min", type=float, default=1.0,
        help="Sanity check: minimum filtration level (Microns)",
    )
    p.add_argument(
        "--filt-max", type=float, default=100.0,
        help="Sanity check: maximum filtration level (Microns)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    vendor_emails = (
        [e.strip() for e in args.vendor_emails.split(",") if e.strip()]
        if args.vendor_emails else []
    )
    thresholds = {
        "flow_min": args.flow_min,
        "flow_max": args.flow_max,
        "press_min": args.press_min,
        "press_max": args.press_max,
        "temp_min": args.temp_min,
        "temp_max": args.temp_max,
        "filt_min": args.filt_min,
        "filt_max": args.filt_max,
    }

    result = run(
        input_dir=args.input,
        output_dir=args.output_dir,
        gemini_key=args.gemini_key,
        groq_key=args.groq_key,
        vendor_file=args.vendor_file,
        vendor_emails=vendor_emails,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user,
        smtp_pass=args.smtp_pass,
        thresholds=thresholds,
    )

    if result.get("status") == "no_input":
        sys.exit(1)

    print("\n── Pipeline Summary ──────────────────────────────────────")
    print(f"  Lead No.       : {result.get('lead_no', 'N/A')}")
    print(f"  RFQ Ref        : {result.get('rfq_ref', 'N/A')}")
    print(f"  Project        : {result.get('project', 'N/A')}")
    print(f"  Customer       : {result.get('customer', 'N/A')}")
    print(f"  BOM rows       : {result.get('bom_rows_total', 0)} total  "
          f"/ {result.get('bom_rows_applicable', 0)} applicable  "
          f"/ {result.get('bom_rows_mandatory', 0)} mandatory")
    v = result.get("validation", {})
    print(f"  Confidence     : {v.get('confidence_score', 0):.1f}%")
    print(f"  Warnings       : {len(v.get('warnings', []))}")
    print()
    files = result.get("output_files", {})
    for label, path in files.items():
        if path:
            print(f"  {label:<26}: {path}")
    print("──────────────────────────────────────────────────────────\n")
