"""
src/output/csv_writer.py
=========================
Phase 4 CSV generation — two output files:

  1. BOM_<lead>_<rfq>_<project>_<customer>_<date>.csv
     Full Annexure-I internal format (28 columns) with header block.
     Matches Cenlub's standard BOM sheet exactly.

  2. SPARES_SUMMARY_<lead>_<rfq>.csv
     Customer-facing stripped version (no prices, no internal codes).
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Optional

from src.core.models import BOMNode, ChecklistDoc, EmailMetadata, RMCData


# ── Annexure-I column order (Phase 4.2) ──────────────────────────────────────

ANNEXURE_HEADERS = [
    "SR.NO",
    "CUST.PART.NO",
    "REV",
    "QTY",
    "UNIT",
    "ITEM (Description)",
    "PLAIN DESCRIPTION",
    "TECHNICAL SPECIFICATION",
    "MOC",
    "MAKE",
    "UNIT PRICE",
    "TOTAL PRICE",
    "DELIVERY TIME (WEEKS)",
    "OFFER REF",
    "VALIDITY DATE",
    "CATEGORY CODE",
    "CATEGORY LABEL",
    "ASSEMBLY GROUP",
    "INTERNAL ITEM CODE",
    "APPLICABLE",
    "MANDATORY",
    "QTY ZERO FLAG",
    "REMARKS",
    "DATA COMPLETE",
    "SOURCE PDF",
    "LEAD NO",
    "CUSTOMER RFQ REF",
    "PROJECT NAME",
]


def _row_to_annexure(r: Dict) -> List:
    """Map a BOMRow dict to the Annexure-I column order."""
    return [
        r.get("sl_no", ""),
        r.get("cust_part_no", ""),
        r.get("rev", ""),
        r.get("qty", ""),
        r.get("unit", ""),
        r.get("item_description", ""),
        r.get("plain_description", ""),
        r.get("technical_specification", ""),
        r.get("moc", ""),
        r.get("make", ""),
        r.get("unit_price", ""),
        r.get("total_price", ""),
        r.get("delivery_weeks", ""),
        r.get("offer_ref", ""),
        r.get("validity_date", ""),
        r.get("category_code", ""),
        r.get("category_label", ""),
        r.get("assembly_group", "GENERAL"),
        r.get("internal_item_code", ""),
        "Y" if r.get("applicable_flag", True) else "N",
        "Y" if r.get("mandatory_flag", False) else "N",
        "Y" if r.get("qty_zero_flag", False) else "N",
        r.get("remarks", ""),
        "Y" if r.get("data_complete_flag", True) else "N",
        r.get("source_pdf", ""),
        r.get("lead_no", ""),
        r.get("customer_rfq_ref", ""),
        r.get("project_name", ""),
    ]


# ── Phase 4.1: Header block ───────────────────────────────────────────────────

def _write_header_block(w: csv.writer, rmc: RMCData,
                        meta: Optional[EmailMetadata] = None) -> None:
    """Write the lead-level header rows at the top of the CSV (Phase 4.1)."""
    lead = rmc.lead_no if rmc.lead_no != "XX" else (meta.lead_no if meta else "")
    proj = rmc.project_name if rmc.project_name != "XX" else (meta.project_name if meta else "")
    cust = rmc.customer if rmc.customer != "XX" else (meta.customer_name if meta else "")
    date = meta.date_received if meta else ""
    rfq = meta.customer_rfq_ref if meta else ""

    w.writerow(["## CENLUB SYSTEMS — BILL OF MATERIALS (ANNEXURE-I)"])
    w.writerow(["LEAD NO.", lead])
    w.writerow(["RFQ REF", rfq])
    w.writerow(["PROJECT NAME", proj])
    w.writerow(["CUSTOMER", cust])
    w.writerow(["DATE RECEIVED", date])
    w.writerow(["FLUID", rmc.oil_grade])
    w.writerow(["FLOW RATE", rmc.flow_rate])
    w.writerow(["OPERATING PRESSURE", rmc.pressure])
    w.writerow(["OPERATING TEMPERATURE", rmc.temperature])
    w.writerow(["FILTRATION LEVEL", rmc.filtration_level])
    w.writerow(["HEAT EXCHANGER MOC", rmc.heat_exchanger_moc])
    w.writerow(["RESERVOIR MOC", rmc.reservoir_moc])
    w.writerow(["CONTROL PANEL", rmc.control_panel_requirement])
    w.writerow(["CIRCUIT", "LUBE OIL SYSTEM"])
    w.writerow(["PREPARED BY", "ASHISH MON"])
    w.writerow([])


# ── Main writer ───────────────────────────────────────────────────────────────

def write_annexure_csv(
    rmc: RMCData,
    bom_rows: List[Dict],
    bom_root: BOMNode,
    warnings: List[str],
    out_path: str,
    meta: Optional[EmailMetadata] = None,
) -> None:
    """
    Phase 4.6: Write the full internal Annexure-I BOM CSV.

    Structure:
      Header block  (lead metadata)
      Annexure-I item rows  (one per BOM/spare item)
      BOM equipment tree section  (hierarchical equipment list)
      Validation warnings section
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # utf-8-sig gives Excel-compatible BOM marker
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)

        # ── Phase 4.1: Header block ───────────────────────────────────────
        _write_header_block(w, rmc, meta)

        # ── Phase 4.2: Item rows ──────────────────────────────────────────
        if bom_rows:
            w.writerow(ANNEXURE_HEADERS)
            for r in bom_rows:
                row_data = _row_to_annexure(r)
                # Phase 4.4: flag Qty=0 rows
                if r.get("qty_zero_flag"):
                    row_data[-3] = "N"  # DATA_COMPLETE = N
                w.writerow(row_data)
        else:
            # Fallback: write RMC + spares in simple format if no structured rows
            w.writerow(["## SECTION A – RMC PARAMETERS"])
            w.writerow(["Parameter", "Extracted Value"])
            for param, val in [
                ("Lead Number", rmc.lead_no),
                ("Project Name", rmc.project_name),
                ("Customer Name", rmc.customer),
                ("Application", rmc.application),
                ("Industry", rmc.industry),
                ("Flow Rate", rmc.flow_rate),
                ("Operating Pressure", rmc.pressure),
                ("Operating Temperature", rmc.temperature),
                ("Oil Grade", rmc.oil_grade),
                ("Filtration Level", rmc.filtration_level),
                ("Heat Exchanger MOC", rmc.heat_exchanger_moc),
                ("Reservoir MOC", rmc.reservoir_moc),
                ("Control Panel Requirement", rmc.control_panel_requirement),
            ]:
                w.writerow([param, val])
            w.writerow([])

            w.writerow(["## SECTION B – SPARES LISTS"])
            w.writerow(["Spare Type", "Item No", "Description", "Qty", "Part No / Frequency"])
            for s in rmc.mandatory_spares:
                w.writerow(["Mandatory Spare", s.item_no, s.description, s.qty, s.part_no])
            for s in rmc.om_spares:
                w.writerow(["O&M Spare", s.item_no, s.description, s.qty, s.frequency])
            if not rmc.mandatory_spares and not rmc.om_spares:
                w.writerow(["–", "–", "No spares extracted", "–", "–"])
            w.writerow([])

        # ── BOM Equipment Tree section ────────────────────────────────────
        w.writerow([])
        w.writerow(["## EQUIPMENT BOM TREE"])
        w.writerow(["Level", "Item No", "Tag", "Equipment / Component Name",
                    "Qty", "Material / MOC", "Unit Price", "Source PDF"])
        if bom_root.children:
            for eq in bom_root.children:
                w.writerow([
                    "Equipment",
                    eq.data.get("item_no", ""),
                    eq.data.get("equipment_tag", ""),
                    eq.name,
                    eq.data.get("qty", ""),
                    eq.data.get("material_specification", ""),
                    eq.data.get("unit_price", ""),
                    eq.data.get("source_file", ""),
                ])
                for ci, comp in enumerate(eq.children):
                    w.writerow([
                        "  └─ Component",
                        f"{eq.data.get('item_no', '')}.{ci + 1}",
                        "",
                        f"      {comp.name}",
                        comp.data.get("qty", ""),
                        comp.data.get("material", ""),
                        "", "",
                    ])
        else:
            w.writerow(["–", "–", "–", "No BOM equipment data", "–", "–", "–", "–"])

        # ── Validation warnings ───────────────────────────────────────────
        w.writerow([])
        w.writerow(["## VALIDATION WARNINGS"])
        w.writerow(["#", "Warning"])
        if warnings:
            for i, warn in enumerate(warnings, 1):
                w.writerow([i, warn])
        else:
            w.writerow(["–", "All checks passed. No warnings."])


# ── Phase 4.7: Customer-facing spares summary ─────────────────────────────────

def write_spares_summary_csv(
    bom_rows: List[Dict],
    rmc: RMCData,
    out_path: str,
    meta: Optional[EmailMetadata] = None,
) -> None:
    """
    Phase 4.7: Write the customer-facing spares summary CSV.
    Contains only: Sl.No, Category, Part Description, Plain Description,
    Quantity, Unit, Remarks, Applicable (Y/N).
    No internal prices, codes, or vendor data.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)

        # Mini header
        lead = rmc.lead_no if rmc.lead_no != "XX" else ""
        rfq = meta.customer_rfq_ref if meta else ""
        w.writerow(["SPARES SUMMARY", f"Lead: {lead}", f"RFQ: {rfq}"])
        w.writerow([])
        w.writerow(["Sl.No", "Category", "Part Description",
                    "Plain Description", "Qty", "Unit", "Remarks", "Applicable"])

        if bom_rows:
            for r in bom_rows:
                w.writerow([
                    r.get("sl_no", ""),
                    f"{r.get('category_code', '')} – {r.get('category_label', '')}".strip(" –"),
                    r.get("item_description", ""),
                    r.get("plain_description", ""),
                    r.get("qty", ""),
                    r.get("unit", ""),
                    r.get("remarks", ""),
                    "Y" if r.get("applicable_flag", True) else "N",
                ])
        else:
            # Fallback: write from RMC spares lists
            for s in rmc.mandatory_spares:
                w.writerow([s.item_no, "Mandatory Spare", s.description,
                            "", s.qty, "", s.part_no, "Y"])
            for s in rmc.om_spares:
                w.writerow([s.item_no, "O&M Spare", s.description,
                            "", s.qty, "", s.frequency, "Y"])
            if not rmc.mandatory_spares and not rmc.om_spares:
                w.writerow(["–", "–", "No spares data", "", "–", "", "", "–"])


# ── Checklist CSV export (Phase 5.4) ─────────────────────────────────────────

def write_checklist_csv(checklist: ChecklistDoc, out_path: str) -> None:
    """Phase 5.4: Export the internal document checklist as CSV."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    doc_items = [
        ("P&ID Availability", checklist.pid_drawing),
        ("GA Drawing", checklist.ga_drawing),
        ("Customer TS for LOS", checklist.customer_ts_los),
        ("Vendor List Availability", checklist.vendor_list),
        ("Mechanical Items Specification", checklist.mech_spec),
        ("Electrical Items Specification", checklist.elec_spec),
        ("Instrumentation Specification", checklist.instrumentation_spec),
        ("PMS (Piping Material Spec)", checklist.pms),
        ("Other Documents", checklist.other_docs),
        ("Control Narrative", checklist.control_narrative),
        ("Welding Specification", checklist.welding_spec),
        ("Painting Specification", checklist.painting_spec),
        ("Packing Specification", checklist.packing_spec),
        ("Water Properties", checklist.water_properties),
        ("Geological Conditions", checklist.geological_conditions),
    ]
    los_params = [
        ("Application", checklist.los_application),
        ("Industry", checklist.los_industry),
        ("EPC", checklist.los_epc),
        ("Consultant", checklist.los_consultant),
        ("Project", checklist.los_project),
        ("Pressure at System Outlet", checklist.los_pressure_outlet),
        ("Pressure at Pump Outlet", checklist.los_pressure_pump),
        ("Flow Rate (LPM)", checklist.los_flow_rate),
        ("Oil Grade", checklist.los_oil_grade),
        ("Temperature Oil In", checklist.los_temp_oil_in),
        ("Temperature Oil Out", checklist.los_temp_oil_out),
        ("Temperature Water In", checklist.los_temp_water_in),
        ("Temperature Water Out", checklist.los_temp_water_out),
        ("MOC Heat Exchanger", checklist.los_moc_heat_exchanger),
        ("Filtration Micron", checklist.los_filtration_micron),
        ("Transmitter (Y/N)", checklist.los_transmitter),
        ("Control Panel (Y/N)", checklist.los_control_panel),
        ("Junction Box (Y/N)", checklist.los_junction_box),
        ("LCS (Y/N)", checklist.los_lcs),
        ("Tank (Y/N)", checklist.los_tank),
        ("MOC of Reservoir", checklist.los_moc_reservoir),
        ("Area Zone", checklist.los_area_zone),
    ]

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["CENLUB ENQUIRY CHECKLIST — CS/DES/O/F/04"])
        w.writerow(["Lead No", checklist.lead_no])
        w.writerow(["Date", checklist.date])
        w.writerow(["Prepared By", checklist.prepared_by])
        w.writerow(["Approved By", checklist.approved_by])
        w.writerow([])
        w.writerow(["## DOCUMENT STATUS"])
        w.writerow(["Sr.No", "Document", "Status"])
        for i, (doc, status) in enumerate(doc_items, 1):
            w.writerow([i, doc, status])
        w.writerow([])
        w.writerow(["## LOS PARAMETERS"])
        w.writerow(["Parameter", "Value"])
        for param, val in los_params:
            w.writerow([param, val])
