from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os
import shutil
import logging
import json
from typing import List, Dict, Any

from app.ingestion.loader import extract_text_from_pdf, extract_tables_from_pdf
from app.ingestion.ocr import perform_ocr_on_page
from app.extraction.classifier import classify_text
from app.extraction.extractor import extract_fields_from_text, parse_spares_table, split_email_thread
from app.schema.models import RMCData, validate_and_analyze_rmc
from app.generators.txt_generator import generate_txt_output
from app.generators.excel_generator import generate_excel_output
from app.rag.retriever import RAGIndex, HistoricalIndex
from app.llm.gemini_client import extract_rmc_with_gemini, audit_rmc_with_critic, answer_query_with_rag

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SmartRMC-Main")

app = FastAPI(title="Smart RMC AI")

# Define directories
RAW_DIR = "data/raw"
PROCESSED_DIR = "data/processed"
STATIC_DIR = "app/static"

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

# Global states
rag_index = RAGIndex()
historical_index = HistoricalIndex()
current_rmc_data = None
current_validation = None
processed_docs = []
current_thresholds = {
    "flow_min": 5.0,
    "flow_max": 2000.0,
    "press_min": 1.0,
    "press_max": 50.0,
    "temp_min": 10.0,
    "temp_max": 100.0,
    "filt_min": 1.0,
    "filt_max": 100.0
}


class QueryRequest(BaseModel):
    query: str


def init_historical_data():
    """Phase 11: Indexes sample validated RMC data for few-shot in-context RAG learning."""
    mock_rfq_history = """
    Lead No: P10020030
    Project Name: METROINDS
    Customer: ASHOK LEYLAND
    Industry: Automotive
    Application: Hydraulic System Skid
    Operating parameters:
    Flow Rate: 40 LPM
    Pressure: 12 Bar
    Temperature: 50 Deg C
    Oil Grade: ISO VG 32
    Filtration Level: 5 Microns
    Reservoir MOC: Mild Steel
    Heat Exchanger MOC: Copper tubes
    Control Panel: PLC based
    """

    mock_rmc_json = {
        "lead_no": "P10020030",
        "project_name": "METROINDS",
        "customer": "ASHOK LEYLAND",
        "application": "Hydraulic System Skid",
        "industry": "Automotive",
        "flow_rate": "40 LPM",
        "pressure": "12 Bar",
        "temperature": "50 Deg C",
        "oil_grade": "ISO VG 32",
        "filtration_level": "5 Microns",
        "heat_exchanger_moc": "Copper",
        "reservoir_moc": "Carbon Steel",
        "control_panel_requirement": "PLC based"
    }

    historical_index.add_historical_record(
        mock_rfq_history, json.dumps(
            mock_rmc_json, indent=2))
    logger.info("Initialized few-shot historical quotation index successfully.")


@app.on_event("startup")
def startup_event():
    init_historical_data()


def merge_extracted_fields(
        all_extractions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merges parameters extracted across different documents based on source class priorities:
    Email > LOS Specification > Technical Specification > BOM > Unknown
    For technical values: LOS Spec > Tech Spec > Email.
    """
    merged = {
        "lead_no": "XX",
        "project_name": "XX",
        "customer": "XX",
        "application": "XX",
        "industry": "XX",
        "flow_rate": "XX",
        "pressure": "XX",
        "temperature": "XX",
        "oil_grade": "XX",
        "filtration_level": "XX",
        "heat_exchanger_moc": "XX",
        "reservoir_moc": "XX",
        "control_panel_requirement": "XX",
    }

    metadata_fields = [
        "lead_no",
        "project_name",
        "customer",
        "industry",
        "application"]
    source_class_for_field = {k: "Unknown" for k in merged.keys()}

    class_ranks = {
        "Email": 5,
        "LOS Specification": 4,
        "Technical Specification": 3,
        "BOM": 2,
        "Unknown": 1,
        "Drawings": 1
    }

    tech_class_ranks = {
        "LOS Specification": 5,
        "Technical Specification": 4,
        "Email": 3,
        "BOM": 2,
        "Unknown": 1,
        "Drawings": 1
    }

    for ext in all_extractions:
        doc_class = ext["classification"]
        fields = ext["fields"]

        for key, val in fields.items():
            if not val or val == "XX":
                continue

            current_val = merged[key]
            current_src = source_class_for_field[key]

            ranks = class_ranks if key in metadata_fields else tech_class_ranks

            new_rank = ranks.get(doc_class, 1)
            old_rank = ranks.get(current_src, 0)

            if current_val == "XX" or new_rank > old_rank:
                merged[key] = val
                source_class_for_field[key] = doc_class

    return merged


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
    filt_max: float = Form(100.0)
):
    """
    Accepts multiple PDF files, saves them, processes the pipeline,
    and returns extracted RMC parameters.
    """
    global rag_index, current_rmc_data, current_validation, processed_docs

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    logger.info(f"Processing {len(files)} files...")

    rag_index = RAGIndex()
    processed_docs = []

    saved_paths = []
    for file in files:
        file_path = os.path.join(RAW_DIR, file.filename)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        saved_paths.append(file_path)
        logger.info(f"Saved: {file.filename}")

    # Phase 1-5: Read and Parse each document
    all_extractions = []
    combined_mandatory_spares = []
    combined_om_spares = []
    all_docs_metadata = []
    combined_text_list = []
    tables_by_doc = {}

    for path in saved_paths:
        doc_data = extract_text_from_pdf(path)
        pdf_name = doc_data["pdf_name"]

        # Run OCR if pages are scanned (Phase 3)
        for page in doc_data["pages"]:
            if page["is_scanned"]:
                logger.info(
                    f"Scanned page detected on {pdf_name} Page {
                        page['page_no']}. Running OCR fallback...")
                page["text"] = perform_ocr_on_page(path, page["page_no"])

        doc_data["full_text"] = "\n\n".join(
            p["text"] for p in doc_data["pages"])
        combined_text_list.append(doc_data["full_text"])

        # Extract Tables (Phase 2)
        tables = extract_tables_from_pdf(path)
        tables_by_doc[pdf_name] = tables

        # Classify document
        doc_class = classify_text(doc_data["full_text"])
        logger.info(f"Classified {pdf_name} as '{doc_class}'")

        # Phase 6: Email Thread Segmentation
        if doc_class == "Email":
            email_segments = split_email_thread(doc_data["full_text"])
            logger.info(
                f"Segmented email {pdf_name} into {
                    len(email_segments)} thread components.")
            # Process segments chronologically (oldest to newest) so newer
            # overwrite older
            for idx, segment in enumerate(reversed(email_segments)):
                extracted_fields = extract_fields_from_text(segment)
                all_extractions.append({
                    "classification": "Email",
                    "fields": extracted_fields
                })
        else:
            extracted_fields = extract_fields_from_text(doc_data["full_text"])
            all_extractions.append({
                "classification": doc_class,
                "fields": extracted_fields
            })

        # Spares Extraction
        if doc_class == "Mandatory Spares":
            for table in tables:
                spares = parse_spares_table(table["data"], "mandatory")
                combined_mandatory_spares.extend(spares)
        elif doc_class == "O&M Spares":
            for table in tables:
                spares = parse_spares_table(table["data"], "om")
                combined_om_spares.extend(spares)
        else:
            for table in tables:
                if table["data"] and len(table["data"]) > 0:
                    headers_str = " ".join(str(h).lower()
                                           for h in table["data"][0])
                    if "mandatory spare" in headers_str or (
                            "spare" in headers_str and "part" in headers_str and "mand" in headers_str):
                        spares = parse_spares_table(table["data"], "mandatory")
                        combined_mandatory_spares.extend(spares)
                    elif "o&m" in headers_str or "recommended spare" in headers_str or "operational" in headers_str:
                        spares = parse_spares_table(table["data"], "om")
                        combined_om_spares.extend(spares)

        all_docs_metadata.append({
            "filename": pdf_name,
            "classification": doc_class,
            "page_count": len(doc_data["pages"])
        })
        processed_docs.append(doc_data)

    rule_based_merged = merge_extracted_fields(all_extractions)
    rule_based_merged["mandatory_spares"] = combined_mandatory_spares
    rule_based_merged["om_spares"] = combined_om_spares

    # Setup RAG index
    rag_index.add_documents(processed_docs, tables_by_doc)

    # Phase 11: Get Few-Shot Example from Historical Quotations Index
    combined_full_text = "\n\n".join(combined_text_list)
    few_shot_example = historical_index.get_similar_example(combined_full_text)
    if few_shot_example:
        logger.info(
            "Found similar historical tender layout. Injecting few-shot example context.")

    # Phase 9: Dual-Agent Critic-Auditor Loop
    logger.info("Triggering Extractor Agent pass...")
    initial_rmc_enhanced = extract_rmc_with_gemini(
        combined_full_text, rule_based_merged, few_shot_example)

    logger.info("Triggering Critic-Auditor Agent validation pass...")
    final_rmc_enhanced = audit_rmc_with_critic(
        combined_full_text, initial_rmc_enhanced)

    # Unpack flat values for output builders
    flat_data = {
        "mandatory_spares": final_rmc_enhanced.mandatory_spares,
        "om_spares": final_rmc_enhanced.om_spares
    }

    grounding_data = {}

    for field in RMCData.model_fields.keys():
        if field in ["mandatory_spares", "om_spares"]:
            continue
        param_obj = getattr(final_rmc_enhanced, field)
        flat_data[field] = param_obj.value
        grounding_data[field] = {
            "quote": param_obj.quote,
            "reasoning": param_obj.reasoning,
            "quote_verified": False
        }

    # Standard validators run inside RMCData (Phase 8: MOC normalizations
    # happen here)
    final_rmc = RMCData(**flat_data)
    current_rmc_data = final_rmc

    # Save the current custom thresholds
    global current_thresholds
    current_thresholds = {
        "flow_min": flow_min,
        "flow_max": flow_max,
        "press_min": press_min,
        "press_max": press_max,
        "temp_min": temp_min,
        "temp_max": temp_max,
        "filt_min": filt_min,
        "filt_max": filt_max
    }

    # Validation & Sanity Checking Layer (Phase 7:
    # run_engineering_sanity_checks runs here)
    validation_results = validate_and_analyze_rmc(
        final_rmc, current_thresholds)

    # Quote Verification Check
    combined_text_lower = combined_full_text.lower()
    for field_name, ground in grounding_data.items():
        quote = ground["quote"]
        val = flat_data[field_name]

        if val != "XX" and quote and quote != "Not Found":
            clean_quote = quote.strip('"\'').lower().strip()
            if clean_quote in combined_text_lower:
                ground["quote_verified"] = True
            else:
                validation_results["warnings"].append(
                    f"Hallucination warning: Quote cited for '{field_name}' ('{quote}') was not found in the source text.")
                validation_results["confidence_score"] = max(
                    0.0, validation_results["confidence_score"] - 5.0)

    current_validation = validation_results

    # Phase 4 & 5: Output Files Generation
    # JSON
    json_path = os.path.join(PROCESSED_DIR, "rmc_result.json")
    with open(json_path, "w") as jf:
        audit_output = {
            "rmc_data": final_rmc.model_dump(),
            "grounding": grounding_data,
            "validation": validation_results
        }
        json.dump(audit_output, jf, indent=2)

    # TXT
    txt_path = os.path.join(PROCESSED_DIR, "rmc_result.txt")
    with open(txt_path, "w") as tf:
        tf.write(generate_txt_output(final_rmc))

    # Excel
    xlsx_path = os.path.join(PROCESSED_DIR, "rmc_result.xlsx")
    generate_excel_output(final_rmc, xlsx_path)

    logger.info("Extraction and file generation completed successfully.")

    return {
        "status": "success",
        "documents": all_docs_metadata,
        "rmc_data": final_rmc,
        "validation": validation_results,
        "grounding": grounding_data
    }


@app.post("/export-modified/")
async def export_modified(data: RMCData):
    """
    Accepts edited RMCData, recalculates validation constraints under current thresholds,
    regenerates output files (Excel, CSV, TXT, JSON), and returns the new validation results.
    """
    global current_rmc_data, current_validation

    current_rmc_data = data

    # Recalculate validation under active thresholds
    validation_results = validate_and_analyze_rmc(data, current_thresholds)
    current_validation = validation_results

    # Regenerate files
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    # JSON
    json_path = os.path.join(PROCESSED_DIR, "rmc_result.json")
    with open(json_path, "w") as jf:
        audit_output = {
            "rmc_data": data.model_dump(),
            "grounding": {},  # Reset/clear grounding for user modifications
            "validation": validation_results
        }
        json.dump(audit_output, jf, indent=2)

    # TXT
    txt_path = os.path.join(PROCESSED_DIR, "rmc_result.txt")
    with open(txt_path, "w") as tf:
        tf.write(generate_txt_output(data))

    # Excel
    xlsx_path = os.path.join(PROCESSED_DIR, "rmc_result.xlsx")
    generate_excel_output(data, xlsx_path)

    logger.info(
        "Regenerated all files successfully from user edited parameters.")

    return {
        "status": "success",
        "validation": validation_results,
        "rmc_data": data
    }


@app.post("/upload-bom-csv/")
async def upload_bom_csv(file: UploadFile = File(...)):
    """
    Accepts a single BOM PDF file, processes it through the hierarchical
    BOM pipeline, structures it into an equipment-component tree,
    and returns the final outcome directly as CSV text.
    """
    import csv
    from io import StringIO

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400,
                            detail="Only PDF files are supported.")

    logger.info(f"Processing uploaded BOM: {file.filename}")

    # Save the file temporarily
    temp_path = os.path.join(RAW_DIR, f"temp_bom_{file.filename}")
    try:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Ingest text and tables
        from run_bom_pipeline import (
            get_pdf_content_for_llm,
            extract_bom_with_groq,
            extract_bom_with_gemini,
            extract_bom_rule_based,
            split_combined_tags,
            clean_currency_and_numbers,
            BOMNode,
            QuadraticProbingHashTable
        )

        content = get_pdf_content_for_llm(temp_path)

        # Get API keys
        groq_key = os.getenv("GROQ_API_KEY")
        gemini_key = os.getenv("GEMINI_API_KEY")

        # Select extractor
        if groq_key:
            logger.info("Extracting BOM using Groq Llama 3.1 8B...")
            bom_structure = extract_bom_with_groq(content, groq_key)
        elif gemini_key:
            logger.info("Groq key missing. Falling back to Gemini...")
            bom_structure = extract_bom_with_gemini(content, gemini_key)
        else:
            logger.info(
                "No API keys found. Falling back to Rule-based parser...")
            bom_structure = extract_bom_rule_based(content)

        # Initialize custom structures
        seen_equipments = QuadraticProbingHashTable()
        root_node = BOMNode("Uploaded BOM", node_type="root")

        # Build hierarchy and check duplicates
        for eq in bom_structure.equipment_list:
            clean_name = eq.description.strip().lower()
            tags_to_check = split_combined_tags(
                eq.equipment_tag) if eq.equipment_tag else [None]

            for tag in tags_to_check:
                sig = f"{clean_name}_{tag or ''}"

                # Duplicate check
                if seen_equipments.contains(sig):
                    continue
                seen_equipments.insert(sig, True)

                cleaned_price = clean_currency_and_numbers(
                    eq.unit_price) if eq.unit_price else ""

                eq_node = BOMNode(
                    name=eq.description,
                    node_type="equipment",
                    data={
                        "item_no": eq.item_no,
                        "qty": eq.qty,
                        "material_specification": eq.material_specification or "",
                        "unit_price": cleaned_price,
                        "equipment_tag": tag or eq.equipment_tag or "",
                        "source_file": file.filename
                    }
                )

                for comp in eq.components:
                    comp_node = BOMNode(
                        name=comp.name,
                        node_type="component",
                        data={
                            "material": comp.material or "",
                            "qty": comp.qty or ""
                        }
                    )
                    eq_node.add_child(comp_node)

                root_node.add_child(eq_node)

        # Generate CSV in memory
        output_buffer = StringIO()
        writer = csv.writer(output_buffer)

        headers = [
            "Level", "Item No", "Tag", "Equipment/Component Name",
            "Qty", "Material / MOC", "Unit Price", "Source PDF"
        ]
        writer.writerow(headers)

        for eq_node in root_node.children:
            writer.writerow([
                "Equipment",
                eq_node.data.get("item_no", ""),
                eq_node.data.get("equipment_tag", ""),
                eq_node.name,
                eq_node.data.get("qty", ""),
                eq_node.data.get("material_specification", ""),
                eq_node.data.get("unit_price", ""),
                eq_node.data.get("source_file", "")
            ])

            for comp_idx, comp_node in enumerate(eq_node.children):
                writer.writerow([
                    "Component",
                    f"{eq_node.data.get('item_no', '')}.{comp_idx + 1}",
                    "",
                    f"  {comp_node.name}",
                    comp_node.data.get("qty", ""),
                    comp_node.data.get("material", ""),
                    "",
                    ""
                ])

        csv_text = output_buffer.getvalue()
        output_buffer.close()

        return {"status": "success", "filename": file.filename, "csv": csv_text}

    except Exception as e:
        logger.error(f"Failed to process BOM PDF: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"BOM extraction failed: {
                str(e)}")

    finally:
        # Clean up temp file
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.post("/query/")
async def query_documents(request: QueryRequest):
    """
    Query documents using RAG vector database retrieval and Gemini.
    """
    if not processed_docs:
        raise HTTPException(
            status_code=400,
            detail="No documents uploaded. Please upload documents first.")

    context_chunks = rag_index.query(request.query, top_k=5)

    if not context_chunks:
        return {
            "answer": "No relevant text chunks found in the index.",
            "citations": []
        }

    answer = answer_query_with_rag(request.query, context_chunks)

    citations = []
    for chunk in context_chunks:
        citations.append({
            "source": chunk["source_file"],
            "page": chunk["page_no"],
            "score": chunk["score"],
            "text": chunk["text"]
        })

    return {
        "answer": answer,
        "citations": citations
    }


@app.get("/download/{file_type}")
async def download_file(file_type: str):
    """
    Downloads the processed files: json, txt, xlsx, or csv.
    """
    if file_type == "json":
        path = os.path.join(PROCESSED_DIR, "rmc_result.json")
        media = "application/json"
    elif file_type == "txt":
        path = os.path.join(PROCESSED_DIR, "rmc_result.txt")
        media = "text/plain"
    elif file_type == "xlsx":
        path = os.path.join(PROCESSED_DIR, "rmc_result.xlsx")
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif file_type == "csv":
        path = os.path.join(PROCESSED_DIR, "rmc_result.csv")
        media = "text/csv"
        # Generate the CSV dynamically from current_rmc_data
        if current_rmc_data:
            import csv
            os.makedirs(PROCESSED_DIR, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(["Parameter", "Value"])
                writer.writerow(["Lead Number", current_rmc_data.lead_no])
                writer.writerow(
                    ["Project Name", current_rmc_data.project_name])
                writer.writerow(["Customer Name", current_rmc_data.customer])
                writer.writerow(["Application", current_rmc_data.application])
                writer.writerow(["Industry", current_rmc_data.industry])
                writer.writerow(["Flow Rate", current_rmc_data.flow_rate])
                writer.writerow(
                    ["Operating Pressure", current_rmc_data.pressure])
                writer.writerow(
                    ["Operating Temperature", current_rmc_data.temperature])
                writer.writerow(["Oil Grade", current_rmc_data.oil_grade])
                writer.writerow(
                    ["Filtration Level", current_rmc_data.filtration_level])
                writer.writerow(
                    ["Heat Exchanger MOC", current_rmc_data.heat_exchanger_moc])
                writer.writerow(
                    ["Reservoir MOC", current_rmc_data.reservoir_moc])
                writer.writerow(["Control Panel Requirement",
                                 current_rmc_data.control_panel_requirement])

                writer.writerow([])
                writer.writerow(["Spares Classification",
                                 "Item No",
                                 "Description",
                                 "Quantity",
                                 "Part No / Frequency"])
                for s in current_rmc_data.mandatory_spares:
                    writer.writerow(
                        ["Mandatory Spare", s.item_no, s.description, s.qty, s.part_no])
                for s in current_rmc_data.om_spares:
                    writer.writerow(
                        ["O&M Spare", s.item_no, s.description, s.qty, s.frequency])
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Support: json, txt, xlsx, csv")

    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail="File not found. Please upload documents to generate results first.")

    return FileResponse(path=path, media_type=media,
                        filename=os.path.basename(path))

# Serve SPA


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Smart RMC AI UI is loading...</h1>")

# Mount static folder
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
