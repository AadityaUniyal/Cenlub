from pydantic import BaseModel, Field, field_validator
from typing import List
import re
import logging

logger = logging.getLogger(__name__)


class MandatorySpare(BaseModel):
    item_no: str = Field(default="", description="Item or Serial number")
    description: str = Field(..., description="Description of the spare part")
    qty: str = Field(default="1", description="Quantity required")
    part_no: str = Field(default="", description="Manufacturer part number")


class OMSpare(BaseModel):
    item_no: str = Field(default="", description="Item or Serial number")
    description: str = Field(..., description="Description of the spare part")
    qty: str = Field(default="1", description="Quantity required")
    frequency: str = Field(default="", description="Replacement frequency (e.g. 2 years)")


class GroundedParameter(BaseModel):
    value: str = Field(default="XX", description="Extracted value for the parameter. Use 'XX' if not found.")
    quote: str = Field(
        default="Not Found",
        description="Exact quote from the document text where this value was found. Use 'Not Found' if not found.")
    reasoning: str = Field(
        default="",
        description="Step-by-step reasoning explaining why this value was extracted and how it was resolved in case of conflicts.")


def standardize_moc(moc_text: str) -> str:
    """
    Phase 8: Maps loose material descriptions to standard ASME/ASTM naming conventions.
    """
    if not moc_text or moc_text == "XX":
        return "XX"

    m_clean = moc_text.lower().replace("-", "").replace(" ", "").strip()

    # Check steel standards
    if any(term in m_clean for term in ["ss304", "304ss", "304stainless", "grade304"]):
        return "SS304"
    if any(term in m_clean for term in ["ss316", "316ss", "316stainless", "grade316"]):
        return "SS316"
    if any(term in m_clean for term in ["carbonsteel", "cs", "mildsteel", "ms", "carbon"]):
        return "Carbon Steel"
    if any(term in m_clean for term in ["copper", "cu", "brass", "bronze"]):
        return "Copper"

    return moc_text


class RMCData(BaseModel):
    lead_no: str = Field(default="XX", description="Inquiry Lead Number")
    project_name: str = Field(default="XX", description="Project Name")
    customer: str = Field(default="XX", description="Customer Name")
    application: str = Field(default="XX", description="Application of Lube Skid")
    industry: str = Field(default="XX", description="Target Industry")
    flow_rate: str = Field(default="XX", description="Lube Skid flow rate in LPM")
    pressure: str = Field(default="XX", description="System pressure in Bar")
    temperature: str = Field(default="XX", description="Operating temperature in Deg C")
    oil_grade: str = Field(default="XX", description="Lube oil viscosity grade (e.g., ISO VG 46)")
    filtration_level: str = Field(default="XX", description="Filtration level in microns")
    heat_exchanger_moc: str = Field(default="XX", description="Heat Exchanger Material of Construction")
    reservoir_moc: str = Field(default="XX", description="Oil Reservoir Material of Construction")
    control_panel_requirement: str = Field(default="XX", description="Control panel details (e.g. PLC, Relay, HMI)")
    mandatory_spares: List[MandatorySpare] = Field(default_factory=list)
    om_spares: List[OMSpare] = Field(default_factory=list)

    @field_validator("flow_rate")
    @classmethod
    def normalize_flow_rate(cls, v: str) -> str:
        if v == "XX" or not v:
            return "XX"
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lpm|l/min|liters?\s+per\s+minute)", v, re.IGNORECASE)
        if match:
            return f"{match.group(1)} LPM"
        return v

    @field_validator("temperature")
    @classmethod
    def normalize_temperature(cls, v: str) -> str:
        if v == "XX" or not v:
            return "XX"
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\°|c|deg|degree)", v, re.IGNORECASE)
        if match:
            return f"{match.group(1)} Deg C"
        return v

    @field_validator("pressure")
    @classmethod
    def normalize_pressure(cls, v: str) -> str:
        if v == "XX" or not v:
            return "XX"
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:bar|kg/cm2)", v, re.IGNORECASE)
        if match:
            return f"{match.group(1)} Bar"
        return v

    @field_validator("filtration_level")
    @classmethod
    def normalize_filtration(cls, v: str) -> str:
        if v == "XX" or not v:
            return "XX"
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:micron|microns|μm|mu)", v, re.IGNORECASE)
        if match:
            return f"{match.group(1)} Microns"
        return v

    @field_validator("heat_exchanger_moc", "reservoir_moc")
    @classmethod
    def normalize_moc(cls, v: str) -> str:
        return standardize_moc(v)


class RMCDataEnhanced(BaseModel):
    lead_no: GroundedParameter = Field(default_factory=GroundedParameter)
    project_name: GroundedParameter = Field(default_factory=GroundedParameter)
    customer: GroundedParameter = Field(default_factory=GroundedParameter)
    application: GroundedParameter = Field(default_factory=GroundedParameter)
    industry: GroundedParameter = Field(default_factory=GroundedParameter)
    flow_rate: GroundedParameter = Field(default_factory=GroundedParameter)
    pressure: GroundedParameter = Field(default_factory=GroundedParameter)
    temperature: GroundedParameter = Field(default_factory=GroundedParameter)
    oil_grade: GroundedParameter = Field(default_factory=GroundedParameter)
    filtration_level: GroundedParameter = Field(default_factory=GroundedParameter)
    heat_exchanger_moc: GroundedParameter = Field(default_factory=GroundedParameter)
    reservoir_moc: GroundedParameter = Field(default_factory=GroundedParameter)
    control_panel_requirement: GroundedParameter = Field(default_factory=GroundedParameter)
    mandatory_spares: List[MandatorySpare] = Field(default_factory=list)
    om_spares: List[OMSpare] = Field(default_factory=list)


def run_engineering_sanity_checks(data: RMCData, thresholds: dict = None) -> List[str]:
    """
    Phase 7: Checks extracted values against standard manufacturing thresholds.
    Accepts custom bounds for flow, pressure, temperature, and filtration.
    """
    warnings = []
    t = thresholds or {}

    # 1. Flow Rate bounds
    if data.flow_rate != "XX":
        match = re.search(r"(\d+(?:\.\d+)?)", data.flow_rate)
        if match:
            flow = float(match.group(1))
            flow_min = float(t.get("flow_min", 5.0))
            flow_max = float(t.get("flow_max", 2000.0))
            if flow < flow_min or flow > flow_max:
                warnings.append(
                    f"Sanity check warning: Flow rate of {flow} LPM is outside typical turbine lube skid limits ({flow_min} - {flow_max} LPM).")

    # 2. Operating Pressure bounds
    if data.pressure != "XX":
        match = re.search(r"(\d+(?:\.\d+)?)", data.pressure)
        if match:
            press = float(match.group(1))
            press_min = float(t.get("press_min", 1.0))
            press_max = float(t.get("press_max", 50.0))
            if press < press_min or press > press_max:
                warnings.append(
                    f"Sanity check warning: Pressure of {press} Bar is outside typical operating envelope ({press_min} - {press_max} Bar).")

    # 3. Operating Temperature bounds
    if data.temperature != "XX":
        match = re.search(r"(\d+(?:\.\d+)?)", data.temperature)
        if match:
            temp = float(match.group(1))
            temp_min = float(t.get("temp_min", 10.0))
            temp_max = float(t.get("temp_max", 100.0))
            if temp < temp_min or temp > temp_max:
                warnings.append(
                    f"Sanity check warning: Operating temperature of {temp} Deg C is outside standard envelopes ({temp_min} - {temp_max} Deg C).")

    # 4. Filtration Level bounds
    if data.filtration_level != "XX":
        match = re.search(r"(\d+(?:\.\d+)?)", data.filtration_level)
        if match:
            filt = float(match.group(1))
            filt_min = float(t.get("filt_min", 1.0))
            filt_max = float(t.get("filt_max", 100.0))
            if filt < filt_min or filt > filt_max:
                warnings.append(
                    f"Sanity check warning: Filtration rating of {filt} Microns is unusual (typical: {filt_min} - {filt_max} Microns).")

    return warnings


def validate_and_analyze_rmc(data: RMCData, thresholds: dict = None) -> dict:
    """
    Validates RMCData and performs analysis.
    """
    warnings = []

    # Check fields filled
    filled_fields = 0
    total_fields = 13  # 13 metadata fields
    missing_fields = []

    for field in RMCData.model_fields.keys():
        if field in ["mandatory_spares", "om_spares"]:
            continue
        val = getattr(data, field)
        if val == "XX" or not val:
            missing_fields.append(field)
        else:
            filled_fields += 1

    confidence_score = round((filled_fields / total_fields) * 100, 2)

    # Add ASME Sanity boundary checks (Phase 7)
    sanity_warnings = run_engineering_sanity_checks(data, thresholds)
    warnings.extend(sanity_warnings)

    # Adjust confidence score based on sanity warnings
    if sanity_warnings:
        confidence_score = max(0.0, round(confidence_score - (len(sanity_warnings) * 5), 2))

    # Check duplicate items in spares
    mand_desc_set = {s.description.lower().strip() for s in data.mandatory_spares if s.description}
    om_desc_set = {s.description.lower().strip() for s in data.om_spares if s.description}

    duplicates = mand_desc_set.intersection(om_desc_set)
    if duplicates:
        warnings.append(
            f"Spares overlap: The following parts are listed in BOTH Mandatory and O&M spares: {
                ', '.join(duplicates)}")

    for f in missing_fields:
        warnings.append(f"Missing parameter: Field '{f}' is not extracted (value is XX).")

    return {
        "confidence_score": confidence_score,
        "missing_fields": missing_fields,
        "warnings": warnings,
        "is_valid": len(warnings) == 0
    }
