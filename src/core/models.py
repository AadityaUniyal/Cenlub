"""
src/core/models.py
==================
Single source of truth for ALL Pydantic schemas and data structures used
across the pipeline:
  - EmailMetadata          (Phase 1 — email intake fields)
  - ChecklistDoc           (Phase 5 — CS/DES/O/F/04 checklist)
  - BOMRow                 (Phase 4 — full Annexure-I row)
  - MandatorySpare, OMSpare
  - RMCData                (with field validators for unit normalisation)
  - BOMComponent, BOMEquipment, BOMStructure  (LLM extraction schemas)
  - BOMNode                (in-memory tree node)
  - QuadraticHashTable     (duplicate suppression)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── MOC normaliser (shared) ──────────────────────────────────────────────────

def standardise_moc(raw: str) -> str:
    """Map free-text material names to standard ASME/ASTM labels."""
    if not raw or raw == "XX":
        return "XX"
    c = raw.lower().replace("-", "").replace(" ", "")
    if any(t in c for t in ["ss304", "304ss", "304stainless"]):
        return "SS304"
    if any(t in c for t in ["ss316", "316ss", "316stainless"]):
        return "SS316"
    if any(t in c for t in ["carbonsteel", "mildsteel", "cs", "ms"]):
        return "Carbon Steel"
    if any(t in c for t in ["copper", "brass", "bronze"]):
        return "Copper"
    return raw


# ── Phase 1: Email metadata ───────────────────────────────────────────────────

class EmailMetadata(BaseModel):
    """Structured metadata extracted from an incoming RFQ email."""
    lead_no: str = Field(default="XX", description="Auto-assigned lead number e.g. P26060840")
    customer_rfq_ref: str = Field(default="XX", description="Customer's RFQ reference e.g. C-4758")
    project_name: str = Field(default="XX", description="Project tag e.g. FLOWMORE")
    customer_name: str = Field(default="XX", description="Customer company name")
    contact_person: str = Field(default="XX", description="Customer contact name")
    contact_email: str = Field(default="XX", description="Customer contact email")
    date_received: str = Field(default="XX", description="Date email received e.g. 03-Jun-2026")
    cc_list: str = Field(default="", description="CC recipients (comma-separated)")
    attachments: List[str] = Field(default_factory=list, description="List of PDF attachment names")
    request_action: str = Field(default="", description="Requested action from email body")


# ── Phase 5: Checklist (CS/DES/O/F/04) ───────────────────────────────────────

class ChecklistDoc(BaseModel):
    """
    Internal Cenlub enquiry checklist — CS/DES/O/F/04.
    Tracks document receipt status and extracted LOS parameters.
    """
    lead_no: str = Field(default="XX")
    date: str = Field(default="XX")
    prepared_by: str = Field(default="ASHISH MON")
    approved_by: str = Field(default="")

    # 15 document checklist items (Y/N/MISSING)
    pid_drawing: str = Field(default="MISSING")
    ga_drawing: str = Field(default="MISSING")
    customer_ts_los: str = Field(default="MISSING")
    vendor_list: str = Field(default="MISSING")
    mech_spec: str = Field(default="MISSING")
    elec_spec: str = Field(default="MISSING")
    instrumentation_spec: str = Field(default="MISSING")
    pms: str = Field(default="MISSING")
    other_docs: str = Field(default="MISSING")
    control_narrative: str = Field(default="MISSING")
    welding_spec: str = Field(default="MISSING")
    painting_spec: str = Field(default="MISSING")
    packing_spec: str = Field(default="MISSING")
    water_properties: str = Field(default="MISSING")
    geological_conditions: str = Field(default="MISSING")

    # 20 LOS parameter fields
    los_application: str = Field(default="XX")
    los_industry: str = Field(default="XX")
    los_epc: str = Field(default="XX")
    los_consultant: str = Field(default="XX")
    los_project: str = Field(default="XX")
    los_pressure_outlet: str = Field(default="XX")
    los_pressure_pump: str = Field(default="XX")
    los_flow_rate: str = Field(default="XX")
    los_oil_grade: str = Field(default="XX")
    los_temp_oil_in: str = Field(default="XX")
    los_temp_oil_out: str = Field(default="XX")
    los_temp_water_in: str = Field(default="XX")
    los_temp_water_out: str = Field(default="XX")
    los_moc_heat_exchanger: str = Field(default="XX")
    los_filtration_micron: str = Field(default="XX")
    los_transmitter: str = Field(default="XX")
    los_control_panel: str = Field(default="XX")
    los_junction_box: str = Field(default="XX")
    los_lcs: str = Field(default="XX")
    los_tank: str = Field(default="XX")
    los_moc_reservoir: str = Field(default="XX")
    los_area_zone: str = Field(default="XX")


# ── Phase 4: Full Annexure-I BOM row ─────────────────────────────────────────

# Assembly group choices (Phase 3.3)
ASSEMBLY_GROUPS = [
    "RESERVOIR ASSEMBLY",
    "LUBE OIL ASSEMBLY — MOTOR PUMP ASSEMBLY",
    "HEAT EXCHANGER ASSEMBLY",
    "WATERLINE ASSEMBLY",
    "DUPLEX FILTER ASSEMBLY",
    "MOTOR PUMP ASSEMBLY (EMERGENCY OIL PUMP)",
    "LUBE OIL DELIVERY HEADER",
    "SKID ASSEMBLY",
    "GENERAL",
]

# Internal item code prefixes (Phase 3.2)
ITEM_CODE_PREFIXES = {
    "lube": "L",
    "motor": "M",
    "instrument": "I",
    "electrical": "E",
    "piping": "P",
}


class BOMRow(BaseModel):
    """
    One row in the final Annexure-I CSV output (Phase 4.2).
    Mirrors Cenlub's internal BOM format exactly.
    """
    lead_no: str = Field(default="")
    customer_rfq_ref: str = Field(default="")
    project_name: str = Field(default="")
    customer_name: str = Field(default="")
    date_received: str = Field(default="")

    # Spares classification
    category_code: str = Field(default="", description="A / B / C / D")
    category_label: str = Field(default="", description="e.g. Steam Turbine")
    sl_no: str = Field(default="")
    assembly_group: str = Field(default="GENERAL")
    internal_item_code: str = Field(default="", description="L23, M6, I16 etc.")

    # Core BOM columns (Annexure-I format)
    cust_part_no: str = Field(default="")
    rev: str = Field(default="")
    qty: str = Field(default="")
    unit: str = Field(default="")
    item_description: str = Field(default="")
    plain_description: str = Field(default="", description="LLM-generated plain summary")
    technical_specification: str = Field(default="")
    moc: str = Field(default="")
    make: str = Field(default="")                     # blank → vendor fills
    unit_price: str = Field(default="")              # blank → vendor fills
    total_price: str = Field(default="")             # calculated after vendor quote
    delivery_weeks: str = Field(default="")          # blank → vendor fills
    offer_ref: str = Field(default="")
    validity_date: str = Field(default="")

    # Flags
    applicable_flag: bool = Field(default=True)
    mandatory_flag: bool = Field(default=False)
    qty_zero_flag: bool = Field(default=False)
    data_complete_flag: bool = Field(default=True)

    # Traceability
    remarks: str = Field(default="")
    source_pdf: str = Field(default="")


class MandatorySpare(BaseModel):
    item_no: str = Field(default="", description="Serial / item number")
    description: str = Field(..., description="Part description")
    qty: str = Field(default="1", description="Quantity required")
    part_no: str = Field(default="", description="Manufacturer part number")


class OMSpare(BaseModel):
    item_no: str = Field(default="", description="Serial / item number")
    description: str = Field(..., description="Part description")
    qty: str = Field(default="1", description="Quantity required")
    frequency: str = Field(default="", description="Replacement frequency")


# ── RMC schema ────────────────────────────────────────────────────────────────

class RMCData(BaseModel):
    """Requirement Mapping Checklist — the primary extraction output."""

    lead_no: str = Field(default="XX")
    project_name: str = Field(default="XX")
    customer: str = Field(default="XX")
    application: str = Field(default="XX")
    industry: str = Field(default="XX")
    flow_rate: str = Field(default="XX")
    pressure: str = Field(default="XX")
    temperature: str = Field(default="XX")
    oil_grade: str = Field(default="XX")
    filtration_level: str = Field(default="XX")
    heat_exchanger_moc: str = Field(default="XX")
    reservoir_moc: str = Field(default="XX")
    control_panel_requirement: str = Field(default="XX")
    mandatory_spares: List[MandatorySpare] = Field(default_factory=list)
    om_spares: List[OMSpare] = Field(default_factory=list)

    @field_validator("flow_rate")
    @classmethod
    def _norm_flow(cls, v: str) -> str:
        if v in ("XX", ""):
            return "XX"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lpm|l/min|liters?\s*per\s*minute)", v, re.I)
        return f"{m.group(1)} LPM" if m else v

    @field_validator("temperature")
    @classmethod
    def _norm_temp(cls, v: str) -> str:
        if v in ("XX", ""):
            return "XX"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|deg|c\b)", v, re.I)
        return f"{m.group(1)} Deg C" if m else v

    @field_validator("pressure")
    @classmethod
    def _norm_press(cls, v: str) -> str:
        if v in ("XX", ""):
            return "XX"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:bar|kg/cm2)", v, re.I)
        return f"{m.group(1)} Bar" if m else v

    @field_validator("filtration_level")
    @classmethod
    def _norm_filt(cls, v: str) -> str:
        if v in ("XX", ""):
            return "XX"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:micron|μm)", v, re.I)
        return f"{m.group(1)} Microns" if m else v

    @field_validator("heat_exchanger_moc", "reservoir_moc")
    @classmethod
    def _norm_moc(cls, v: str) -> str:
        return standardise_moc(v)


# ── BOM LLM schemas ───────────────────────────────────────────────────────────

class BOMComponent(BaseModel):
    """Sub-component within a BOM equipment row."""
    name: str = Field(..., description="Component name (e.g. Casing, Rotor, Shell)")
    material: Optional[str] = Field(None, description="Material of construction")
    qty: Optional[str] = Field(None, description="Quantity if specified")


class BOMEquipment(BaseModel):
    """One equipment row in the Bill of Materials."""
    item_no: str = Field(..., description="Item number from table")
    description: str = Field(..., description="Equipment name / description")
    qty: str = Field(..., description="Quantity with unit (e.g. 2 Nos)")
    material_specification: Optional[str] = Field(None, description="Overall MOC")
    unit_price: Optional[str] = Field(None, description="Unit price")
    equipment_tag: Optional[str] = Field(None, description="Tag (e.g. P-101A/B)")
    components: List[BOMComponent] = Field(default_factory=list)


class BOMStructure(BaseModel):
    """Top-level LLM-extracted BOM structure."""
    project_name: Optional[str] = Field(None)
    equipment_list: List[BOMEquipment] = Field(...)


# ── In-memory tree ────────────────────────────────────────────────────────────

class BOMNode:
    """
    N-ary tree node for the hierarchical BOM.
      node_type: 'root' | 'equipment' | 'component'
    """

    def __init__(
        self,
        name: str,
        node_type: str = "equipment",
        data: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.node_type = node_type
        self.data: Dict[str, Any] = data or {}
        self.children: List["BOMNode"] = []

    def add_child(self, child: "BOMNode") -> None:
        self.children.append(child)


# ── Quadratic-probing hash table ──────────────────────────────────────────────

class QuadraticHashTable:
    """
    Open-addressing hash table with quadratic probing.
    Used for O(1) duplicate detection during BOM processing.
    Load factor kept below 0.5 via automatic resizing.
    """

    def __init__(self, capacity: int = 2003):
        self._cap = capacity
        self._slots: List[Optional[str]] = [None] * capacity
        self._size = 0

    # ------------------------------------------------------------------
    def _hash(self, key: str) -> int:
        return abs(hash(key)) % self._cap

    def _resize(self) -> None:
        old = [(s) for s in self._slots if s is not None]
        self._cap = self._cap * 2 + 1
        self._slots = [None] * self._cap
        self._size = 0
        for k in old:
            self.add(k)

    # ------------------------------------------------------------------
    def add(self, key: str) -> bool:
        """
        Insert *key*.
        Returns True  if the key is NEW (was inserted).
        Returns False if the key already existed (duplicate).
        """
        if self._size >= self._cap // 2:
            self._resize()

        start = self._hash(key)
        for i in range(self._cap):
            idx = (start + i * i) % self._cap
            if self._slots[idx] is None:
                self._slots[idx] = key
                self._size += 1
                return True
            if self._slots[idx] == key:
                return False          # duplicate
        return True                   # table full edge-case

    def contains(self, key: str) -> bool:
        start = self._hash(key)
        for i in range(self._cap):
            idx = (start + i * i) % self._cap
            if self._slots[idx] is None:
                return False
            if self._slots[idx] == key:
                return True
        return False
