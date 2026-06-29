"""
src/api/server.py
==================
FastAPI web server — exposes the pipeline via HTTP endpoints.

Endpoints:
  POST /upload/           Process a tender package (multiple PDFs)
  POST /export-modified/  Re-validate and export user-edited RMC data
  POST /upload-bom-csv/   Extract and return BOM CSV from a single PDF
  POST /query/            RAG document chat (requires prior upload)
  GET  /download/{type}   Download result files (csv, xlsx, json, txt)
  GET  /                  Serve the React-like SPA UI
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
from io import StringIO
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.core.models import (
    BOMNode, ChecklistDoc, EmailMetadata,
    MandatorySpare, OMSpare, QuadraticHashTable, RMCData,
)
from src.core.validators import validate_rmc
from src.extraction.bom_extractor import clean_price, extract_bom, split_tags
from src.extraction.classifier import classify
from src.extraction.rmc_extractor import (
    assign_assembly_groups, assign_item_codes, cross_reference_mandatory,
    extract_email_metadata, extract_fields, merge_extractions,
    match_technical_specs, parse_spares_table, parse_spares_table_full,
    split_email_thread,
)
from src.ingestion.loader import load_pdf
from src.llm.gemini_client import (
    answer_query, auditor_agent, extractor_agent,
    fill_checklist_from_text, generate_plain_descriptions,
)
from src.output.csv_writer import (
    write_annexure_csv, write_checklist_csv, write_spares_summary_csv,
)
from src.output.excel_writer import write_excel

log = logging.getLogger("api")

# ── Directories ───────────────────────────────────────────────────────────────
RAW_DIR = "data/raw"
PROCESSED_DIR = "data/processed"
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

for d in (RAW_DIR, PROCESSED_DIR, STATIC_DIR):
    os.makedirs(d, exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Smart RMC AI", version="2.0")

# Global state (single-user assumption — sufficient for an internship project)
_state: Dict[str, Any] = {
    "rmc": None,
    "validation": None,
    "docs": [],
    "rag_index": None,
}

_RMC_FIELDS = [
    "lead_no", "project_name", "customer", "application", "industry",
    "flow_rate", "pressure", "temperature", "oil_grade", "filtration_level",
    "heat_exchanger_moc", "reservoir_moc", "control_panel_requirement",
]

_BOM_KEYWORDS = ["bill of materials", "bom", "unit price", "item no"]


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def _startup():
    _init_rag()


def _init_rag():
    """Initialise FAISS RAG index (lazy import — fails gracefully if not installed)."""
    try:
        from src.rag.retriever import RAGIndex
        _state["rag_index"] = RAGIndex()
        log.info("RAG index initialised.")
    except Exception as exc:
        log.warning(f"RAG index unavailable: {exc}")


# ── Upload & process ──────────────────────────────────────────────────────────
@app.post("/upload/")
async def upload_files(
    files: List[UploadFile] = File(...),
    flow_min: float = Form(5.0),
    flow_max: float = Form(2000.0),
    press_min: float = Form(1.0),
    press_max: float = Form(50.0),
    temp_min: float = Form(10.0),
    temp_max: float = Form(100.0),
    filt_min: float = Form(1.0),
    filt_max: float = Form(100.0),
):
    if not files:
        raise HTTPException(400, "No files uploaded.")

    gemini_key = os.getenv("GEMINI_API_KEY", "")
    _ = os.getenv("GROQ_API_KEY", "")  # Reserved for future use
    thresholds = {
        "flow_min": flow_min, "flow_max": flow_max,
        "press_min": press_min, "press_max": press_max,
        "temp_min": temp_min, "temp_max": temp_max,
        "filt_min": filt_min, "filt_max": filt_max,
    }

    # Save uploads
    saved_paths = []
    for f in files:
        path = os.path.join(RAW_DIR, f.filename)
        with open(path, "wb") as fh:
            shutil.copyfileobj(f.file, fh)
        saved_paths.append(path)

    # Ingest + classify
    docs = [load_pdf(p) for p in saved_paths]
    for doc in docs:
        doc["classification"] = classify(doc["full_text"])

    _state["docs"] = docs

    # Refresh RAG index
    if _state["rag_index"] is not None:
        try:
            _state["rag_index"].add_documents(
                docs,
                {d["pdf_name"]: d["tables"] for d in docs},
            )
        except Exception as exc:
            log.warning(f"RAG indexing failed: {exc}")

    # Rule-based extraction
    all_extractions, mandatory_spares, om_spares, combined_texts = [], [], [], []
    all_docs_meta = []

    for doc in docs:
        cls, text = doc["classification"], doc["full_text"]
        combined_texts.append(text)
        all_docs_meta.append({
            "filename": doc["pdf_name"],
            "classification": cls,
            "page_count": len(doc["pages"]),
        })

        if cls == "Email":
            for seg in reversed(split_email_thread(text)):
                all_extractions.append({"classification": "Email", "fields": extract_fields(seg)})
        else:
            all_extractions.append({"classification": cls, "fields": extract_fields(text)})

        for tbl in doc.get("tables", []):
            data = tbl["data"]
            if not data:
                continue
            hdr = " ".join(data[0]).lower()
            if cls == "Mandatory Spares" or "mandatory" in hdr:
                mandatory_spares.extend(parse_spares_table(data, "mandatory"))
            elif cls == "O&M Spares" or "o&m" in hdr:
                om_spares.extend(parse_spares_table(data, "om"))

    merged = merge_extractions(all_extractions)
    merged["mandatory_spares"] = mandatory_spares
    merged["om_spares"] = om_spares
    combined_text = "\n\n".join(combined_texts)

    # LLM agents
    if gemini_key:
        llm = extractor_agent(combined_text, merged, gemini_key)
        llm = auditor_agent(combined_text, llm, gemini_key)
        for key in _RMC_FIELDS:
            if merged.get(key, "XX") == "XX" and llm.get(key, "XX") != "XX":
                merged[key] = llm[key]
        if not mandatory_spares:
            merged["mandatory_spares"] = llm.get("mandatory_spares", [])
        if not om_spares:
            merged["om_spares"] = llm.get("om_spares", [])

    # Build RMCData
    def _spare(raw, cls_):
        try:
            return cls_(**raw) if isinstance(raw, dict) else raw
        except Exception:
            return None

    rmc = RMCData(**{
        **{k: merged.get(k, "XX") for k in _RMC_FIELDS},
        "mandatory_spares": [s for s in (_spare(r, MandatorySpare) for r in merged.get("mandatory_spares", [])) if s],
        "om_spares": [s for s in (_spare(r, OMSpare) for r in merged.get("om_spares", [])) if s],
    })

    validation = validate_rmc(rmc, thresholds)
    _state["rmc"] = rmc
    _state["validation"] = validation

    # ── Phase 1.2: Email metadata ─────────────────────────────────────────────
    meta = None
    for doc in docs:
        if doc["classification"] == "Email":
            meta_dict = extract_email_metadata(doc["full_text"])
            meta = EmailMetadata(**meta_dict)
            if meta.lead_no == "XX":
                meta.lead_no = rmc.lead_no
            break

    # ── Phase 2A full BOM rows (spares table with category codes) ─────────────
    bom_rows: list = []
    mand_rows: list = []
    lead_no = rmc.lead_no if rmc.lead_no != "XX" else ""
    rfq_ref = meta.customer_rfq_ref if meta else ""
    project = rmc.project_name if rmc.project_name != "XX" else ""
    customer_n = rmc.customer if rmc.customer != "XX" else ""
    date_rcv = meta.date_received if meta else ""

    for doc in docs:
        cls = doc["classification"]
        for tbl in doc.get("tables", []):
            data = tbl["data"]
            if not data:
                continue
            if cls in ("O&M Spares",):
                rows = parse_spares_table_full(
                    data, doc["pdf_name"],
                    lead_no=lead_no, rfq_ref=rfq_ref,
                    project=project, customer=customer_n, date=date_rcv,
                )
                bom_rows.extend(rows)
            elif cls == "Mandatory Spares":
                rows = parse_spares_table_full(
                    data, doc["pdf_name"],
                    lead_no=lead_no, rfq_ref=rfq_ref,
                    project=project, customer=customer_n, date=date_rcv,
                )
                mand_rows.extend(rows)

    # Phase 2D: mandatory cross-reference
    if mand_rows:
        bom_rows = cross_reference_mandatory(bom_rows, mand_rows)

    # Phase 2C: match technical specs per item
    tech_texts = [
        d["full_text"] for d in docs
        if d["classification"] == "Technical Specification"
    ]
    if tech_texts and bom_rows:
        bom_rows = match_technical_specs(bom_rows, "\n\n".join(tech_texts))

    # Phase 2A.3: LLM plain descriptions
    if gemini_key and bom_rows:
        bom_rows = generate_plain_descriptions(bom_rows, gemini_key)

    # Phase 3.2 & 3.3: item codes and assembly groups
    bom_rows = assign_item_codes(bom_rows)
    bom_rows = assign_assembly_groups(bom_rows)

    # Phase 5: checklist
    checklist_dict: dict = {}
    if gemini_key:
        checklist_dict = fill_checklist_from_text(combined_text, gemini_key)

    # Auto-fill doc statuses from classified documents
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
        "RECEIVED" if any(c in cls_names for c in ("O&M Spares", "Mandatory Spares")) else "MISSING",
    )
    checklist_dict["lead_no"] = lead_no or "XX"
    checklist_dict["date"] = date_rcv or "XX"

    checklist = ChecklistDoc(**{
        k: v for k, v in checklist_dict.items()
        if k in ChecklistDoc.model_fields
    })

    # Build BOM tree
    bom_root = BOMNode("root", "root")
    seen = QuadraticHashTable()
    for bom_struct in [extract_bom(doc, "", gemini_key) for doc in docs]:
        if not bom_struct:
            continue
        for eq in bom_struct.equipment_list:
            for tag in (split_tags(eq.equipment_tag) if eq.equipment_tag else [None]):
                sig = f"{eq.description.strip().lower()}_{tag or ''}"
                if not seen.add(sig):
                    continue
                eq_node = BOMNode(eq.description, "equipment", {
                    "item_no": eq.item_no, "qty": eq.qty,
                    "material_specification": eq.material_specification or "",
                    "unit_price": clean_price(eq.unit_price or ""),
                    "equipment_tag": tag or eq.equipment_tag or "",
                    "source_file": eq.item_no,
                })
                for comp in eq.components:
                    eq_node.add_child(BOMNode(
                        comp.name, "component",
                        {"material": comp.material or "", "qty": comp.qty or ""},
                    ))
                bom_root.add_child(eq_node)

    # Output files
    # Phase 4.6: BOM CSV (Annexure-I)
    import re as _re

    def _slug(s: str) -> str:
        return _re.sub(r"[^A-Za-z0-9]+", "", s.upper())[:12]

    bom_fname = "_".join(filter(None, [
        "BOM", lead_no, _slug(rfq_ref), _slug(project),
        _slug(customer_n), _re.sub(r"[^A-Za-z0-9]", "", date_rcv)[:9],
    ])) + ".csv"
    write_annexure_csv(
        rmc, bom_rows, bom_root, validation["warnings"],
        os.path.join(PROCESSED_DIR, bom_fname), meta,
    )
    # Also write the stable name for the download endpoint
    write_annexure_csv(
        rmc, bom_rows, bom_root, validation["warnings"],
        os.path.join(PROCESSED_DIR, "rmc_result.csv"), meta,
    )

    # Phase 4.7: Spares summary CSV
    spares_fname = "_".join(filter(None, [
        "SPARES_SUMMARY", lead_no, _slug(rfq_ref),
    ])) + ".csv"
    write_spares_summary_csv(
        bom_rows, rmc,
        os.path.join(PROCESSED_DIR, spares_fname), meta,
    )

    # Phase 5.4: Checklist CSV
    checklist_fname = "_".join(filter(None, [
        "CHECKLIST", lead_no, _slug(rfq_ref),
        _re.sub(r"[^A-Za-z0-9]", "", date_rcv)[:9],
    ])) + ".csv"
    write_checklist_csv(
        checklist, os.path.join(PROCESSED_DIR, checklist_fname),
    )

    write_excel(rmc, os.path.join(PROCESSED_DIR, "rmc_result.xlsx"))
    with open(os.path.join(PROCESSED_DIR, "rmc_result.json"), "w") as jf:
        json.dump({"rmc_data": rmc.model_dump(), "validation": validation}, jf, indent=2)
    with open(os.path.join(PROCESSED_DIR, "rmc_result.txt"), "w") as tf:
        tf.write(_to_txt(rmc))

    return {
        "status": "success",
        "documents": all_docs_meta,
        "rmc_data": rmc,
        "validation": validation,
    }


# ── Export modified ───────────────────────────────────────────────────────────
@app.post("/export-modified/")
async def export_modified(data: RMCData):
    validation = validate_rmc(data)
    _state["rmc"] = data
    _state["validation"] = validation

    write_annexure_csv(data, [], BOMNode("root", "root"), validation["warnings"],
                       os.path.join(PROCESSED_DIR, "rmc_result.csv"))
    write_excel(data, os.path.join(PROCESSED_DIR, "rmc_result.xlsx"))
    with open(os.path.join(PROCESSED_DIR, "rmc_result.json"), "w") as jf:
        json.dump({"rmc_data": data.model_dump(), "validation": validation}, jf, indent=2)
    with open(os.path.join(PROCESSED_DIR, "rmc_result.txt"), "w") as tf:
        tf.write(_to_txt(data))

    return {"status": "success", "validation": validation, "rmc_data": data}


# ── BOM CSV upload ────────────────────────────────────────────────────────────
@app.post("/upload-bom-csv/")
async def upload_bom_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported.")

    tmp_path = os.path.join(RAW_DIR, f"_tmp_bom_{file.filename}")
    try:
        with open(tmp_path, "wb") as fh:
            shutil.copyfileobj(file.file, fh)

        doc = load_pdf(tmp_path)
        bom_struct = extract_bom(
            doc,
            os.getenv("GROQ_API_KEY", ""),
            os.getenv("GEMINI_API_KEY", ""),
        )
        if not bom_struct:
            raise HTTPException(422, "No BOM content detected in this PDF.")

        seen = QuadraticHashTable()
        bom_root = BOMNode("root", "root")

        for eq in bom_struct.equipment_list:
            for tag in (split_tags(eq.equipment_tag) if eq.equipment_tag else [None]):
                sig = f"{eq.description.strip().lower()}_{tag or ''}"
                if not seen.add(sig):
                    continue
                eq_node = BOMNode(
                    eq.description,
                    "equipment",
                    {
                        "item_no": eq.item_no,
                        "qty": eq.qty,
                        "material_specification": eq.material_specification or "",
                        "unit_price": clean_price(eq.unit_price or ""),
                        "equipment_tag": tag or eq.equipment_tag or "",
                        "source_file": file.filename,
                    },
                )
                for comp in eq.components:
                    eq_node.add_child(BOMNode(
                        comp.name, "component",
                        {"material": comp.material or "", "qty": comp.qty or ""},
                    ))
                bom_root.add_child(eq_node)

        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["Level", "Item No", "Tag", "Equipment/Component Name",
                    "Qty", "Material / MOC", "Unit Price", "Source PDF"])
        for eq_n in bom_root.children:
            w.writerow(["Equipment", eq_n.data.get("item_no", ""),
                        eq_n.data.get("equipment_tag", ""), eq_n.name,
                        eq_n.data.get("qty", ""), eq_n.data.get("material_specification", ""),
                        eq_n.data.get("unit_price", ""), eq_n.data.get("source_file", "")])
            for ci, comp_n in enumerate(eq_n.children):
                w.writerow(["Component", f"{eq_n.data.get('item_no', '')}.{ci + 1}", "",
                            f"  {comp_n.name}", comp_n.data.get("qty", ""),
                            comp_n.data.get("material", ""), "", ""])

        return {"status": "success", "filename": file.filename, "csv": buf.getvalue()}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"BOM extraction failed: {exc}") from exc
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ── RAG query ─────────────────────────────────────────────────────────────────
@app.post("/query/")
async def query_documents(request: dict):
    if not _state["docs"]:
        raise HTTPException(400, "No documents uploaded yet.")
    if _state["rag_index"] is None:
        raise HTTPException(503, "RAG index unavailable (sentence-transformers not installed).")

    query = request.get("query", "")
    chunks = _state["rag_index"].query(query, top_k=5)
    if not chunks:
        return {"answer": "No relevant text found.", "citations": []}

    answer = answer_query(query, chunks, os.getenv("GEMINI_API_KEY", ""))
    return {
        "answer": answer,
        "citations": [
            {"source": c["source_file"], "page": c["page_no"],
             "score": c["score"], "text": c["text"]}
            for c in chunks
        ],
    }


# ── Download ──────────────────────────────────────────────────────────────────
@app.get("/download/{file_type}")
async def download_file(file_type: str):
    _map = {
        "csv": ("rmc_result.csv", "text/csv"),
        "xlsx": ("rmc_result.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "json": ("rmc_result.json", "application/json"),
        "txt": ("rmc_result.txt", "text/plain"),
    }
    if file_type not in _map:
        raise HTTPException(400, f"Unknown type '{file_type}'. Use: csv, xlsx, json, txt")
    filename, media = _map[file_type]
    path = os.path.join(PROCESSED_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found. Upload documents first.")
    return FileResponse(path=path, media_type=media, filename=filename)


# ── SPA UI ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    idx = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    return HTMLResponse("<h1>Smart RMC AI — UI loading…</h1>")


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Text output helper ────────────────────────────────────────────────────────
def _to_txt(data: RMCData) -> str:
    lines = [
        f"Lead Number               : {data.lead_no}",
        f"Project Name              : {data.project_name}",
        f"Customer Name             : {data.customer}",
        f"Application               : {data.application}",
        f"Industry                  : {data.industry}",
        f"Flow Rate                 : {data.flow_rate}",
        f"Operating Pressure        : {data.pressure}",
        f"Operating Temperature     : {data.temperature}",
        f"Oil Grade                 : {data.oil_grade}",
        f"Filtration Level          : {data.filtration_level}",
        f"Heat Exchanger MOC        : {data.heat_exchanger_moc}",
        f"Reservoir MOC             : {data.reservoir_moc}",
        f"Control Panel Requirement : {data.control_panel_requirement}",
        "",
        "Mandatory Spares:",
    ]
    for s in data.mandatory_spares:
        lines.append(f"  * {s.description} [Qty: {s.qty}] (Part: {s.part_no})")
    if not data.mandatory_spares:
        lines.append("  * None listed")
    lines += ["", "O&M Spares:"]
    for s in data.om_spares:
        lines.append(f"  * {s.description} [Qty: {s.qty}] (Freq: {s.frequency})")
    if not data.om_spares:
        lines.append("  * None listed")
    return "\n".join(lines)
