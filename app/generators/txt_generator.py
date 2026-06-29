from app.schema.models import RMCData


def generate_txt_output(data: RMCData) -> str:
    """
    Generates a structured text output matching the client format specifications.
    """
    lines = []
    lines.append(f"Lead Number : {data.lead_no}")
    lines.append(f"Project Name : {data.project_name}")
    lines.append(f"Customer Name : {data.customer}")
    lines.append(f"Application : {data.application}")
    lines.append(f"Industry : {data.industry}")
    lines.append(f"Flow Rate : {data.flow_rate}")
    lines.append(f"Oil Grade : {data.oil_grade}")
    lines.append(f"Pressure : {data.pressure}")
    lines.append(f"Temperature : {data.temperature}")
    lines.append(f"Filtration Level : {data.filtration_level}")
    lines.append(f"Heat Exchanger MOC : {data.heat_exchanger_moc}")
    lines.append(f"Reservoir MOC : {data.reservoir_moc}")
    lines.append(
        f"Control Panel Requirement : {
            data.control_panel_requirement}")
    lines.append("")

    lines.append("Mandatory Spares :")
    if data.mandatory_spares:
        for spare in data.mandatory_spares:
            part_str = f" (Part No: {spare.part_no})" if spare.part_no else ""
            qty_str = f" [Qty: {spare.qty}]" if spare.qty else ""
            lines.append(f"* {spare.description}{qty_str}{part_str}")
    else:
        lines.append("* None Listed")

    lines.append("")
    lines.append("O&M Spares :")
    if data.om_spares:
        for spare in data.om_spares:
            freq_str = f" (Freq: {spare.frequency})" if spare.frequency else ""
            qty_str = f" [Qty: {spare.qty}]" if spare.qty else ""
            lines.append(f"* {spare.description}{qty_str}{freq_str}")
    else:
        lines.append("* None Listed")

    return "\n".join(lines)
