import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def create_pdf(filename, story_elements):
    doc = SimpleDocTemplate(filename, pagesize=letter)
    doc.build(story_elements)


def generate_mock_data(output_dir="data/raw"):
    os.makedirs(output_dir, exist_ok=True)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#1A365D"),
        spaceAfter=15,
    )
    section_style = ParagraphStyle(
        "DocSection",
        parent=styles["Heading2"],
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#2B6CB0"),
        spaceAfter=10,
        spaceBefore=10,
    )
    body_style = ParagraphStyle(
        "DocBody",
        parent=styles["BodyText"],
        fontSize=10,
        leading=14,
        spaceAfter=6,
    )

    # 1. RFQ Email PDF
    rfq_story = [
        Paragraph("RFQ Inquiry Details", title_style),
        Spacer(1, 10),
        Paragraph("<b>Lead No:</b> P26060840", body_style),
        Paragraph("<b>Project Name:</b> FLOWMORE", body_style),
        Paragraph("<b>Customer Name:</b> TATA POWER", body_style),
        Paragraph("<b>Industry:</b> Power Generation", body_style),
        Paragraph(
            "<b>Application:</b> Lube Oil System (LOS)", body_style
        ),
        Spacer(1, 10),
        Paragraph(
            "Dear Team,<br/><br/>Please find attached the tender "
            "specification and requirements for the FLOWMORE project "
            "Lube Oil System. Kindly submit your technical and commercial "
            "offer based on these specifications.<br/><br/>Best "
            "Regards,<br/>Purchasing Team - TATA POWER",
            body_style,
        ),
    ]
    create_pdf(os.path.join(output_dir, "rfq_email.pdf"), rfq_story)
    print("Generated rfq_email.pdf")

    # 2. LOS Specification PDF
    los_story = [
        Paragraph("Lube Oil System (LOS) Specification", title_style),
        Spacer(1, 10),
        Paragraph(
            "1.0 Operating Conditions & Parameters", section_style
        ),
        Paragraph(
            "The lubrication system must satisfy the following criteria:",
            body_style,
        ),
        Paragraph(
            "<b>Application:</b> Steam Turbine Lube Oil Skid", body_style
        ),
        Paragraph("<b>Flow Rate:</b> 50 LPM", body_style),
        Paragraph("<b>Oil Grade:</b> ISO VG 46", body_style),
        Paragraph(
            "<b>Operating Pressure:</b> 10 Bar (Max 16 Bar)", body_style
        ),
        Paragraph(
            "<b>Operating Temperature:</b> 45 Deg C (Max 65 Deg C)",
            body_style,
        ),
        Paragraph(
            "<b>Filtration Level:</b> 10 Microns nominal", body_style
        ),
        Spacer(1, 10),
        Paragraph("2.0 Construction Materials", section_style),
        Paragraph(
            "<b>Reservoir Material of Construction (MOC):</b> SS304",
            body_style,
        ),
        Paragraph(
            "<b>Heat Exchanger MOC (Tubes):</b> Copper", body_style
        ),
        Paragraph(
            "<b>Control Panel Requirement:</b> PLC Based with HMI",
            body_style,
        ),
    ]
    create_pdf(
        os.path.join(output_dir, "los_specification.pdf"), los_story
    )
    print("Generated los_specification.pdf")

    # 3. Technical Specification
    tech_story = [
        Paragraph(
            "Technical Specification - Mechanical Equipments",
            title_style,
        ),
        Spacer(1, 10),
        Paragraph(
            "This document outlines requirements for pumps, motors "
            "and accessories.",
            body_style,
        ),
        Paragraph(
            "<b>Pump Details:</b> Positive Displacement Screw Pump, "
            "Flow: 50 LPM, Speed: 1450 RPM",
            body_style,
        ),
        Paragraph(
            "<b>Motor Details:</b> 3 kW, 4 Pole, IE3 efficiency, "
            "IP55 protection, Class F insulation",
            body_style,
        ),
        Paragraph(
            "<b>Heat Exchanger:</b> Shell & Tube type, "
            "Shell MOC: Carbon Steel, Tubes MOC: Copper",
            body_style,
        ),
    ]
    create_pdf(
        os.path.join(output_dir, "technical_spec.pdf"), tech_story
    )
    print("Generated technical_spec.pdf")

    # 4. BOM PDF
    bom_data = [
        ["Item No", "Description", "Qty", "Material Specification",
         "Unit Price"],
        ["1", "Screw Pump", "2 Nos",
         "Cast Iron Casing, Alloy Steel Rotors", "INR 75,000"],
        ["2", "Electric Motor", "2 Nos", "Cast Iron Frame",
         "INR 35,000"],
        ["3", "Shell & Tube Heat Exchanger", "1 No",
         "Shell: CS, Tubes: Copper", "INR 1,20,000"],
        ["4", "Lube Oil Reservoir", "1 No", "Stainless Steel SS304",
         "INR 2,50,000"],
        ["5", "Duplex Oil Filter", "1 Set", "Carbon Steel housing",
         "INR 80,000"],
    ]
    bom_table = Table(bom_data, colWidths=[60, 180, 50, 130, 80])
    bom_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2B6CB0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    bom_story = [
        Paragraph("Bill of Materials (BOM) Sheet", title_style),
        Spacer(1, 10),
        Paragraph(
            "Below is the list of materials required for the "
            "FLOWMORE project:",
            body_style,
        ),
        Spacer(1, 10),
        bom_table,
    ]
    create_pdf(os.path.join(output_dir, "bom_sheet.pdf"), bom_story)
    print("Generated bom_sheet.pdf")

    # 5. Mandatory Spares PDF
    mand_data = [
        ["Item No", "Description", "Qty", "Part Number"],
        ["1", "Rotor Assembly", "2", "ROT-101"],
        ["2", "Mechanical Seal", "4", "MS-202"],
        ["3", "Bearings", "8", "BRG-303"],
    ]
    mand_table = Table(mand_data, colWidths=[60, 200, 80, 140])
    mand_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#C53030")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    mand_story = [
        Paragraph("Mandatory Spares List", title_style),
        Spacer(1, 10),
        Paragraph(
            "The following mandatory spares must be supplied with "
            "the main equipment:",
            body_style,
        ),
        Spacer(1, 10),
        mand_table,
    ]
    create_pdf(
        os.path.join(output_dir, "mandatory_spares.pdf"), mand_story
    )
    print("Generated mandatory_spares.pdf")

    # 6. O&M Spares PDF
    om_data = [
        ["Item No", "Description", "Qty", "Recommended Frequency"],
        ["1", "Governor", "1", "2 Years"],
        ["2", "Rotor", "1", "5 Years"],
        ["3", "Thrust Bearing", "2", "3 Years"],
        ["4", "Filter Elements", "12", "6 Months"],
    ]
    om_table = Table(om_data, colWidths=[60, 200, 80, 140])
    om_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D69E2E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("GRID", (0, 0), (-1, -1), 1, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    om_story = [
        Paragraph(
            "Operation & Maintenance (O&M) Spares List", title_style
        ),
        Spacer(1, 10),
        Paragraph(
            "Below is the list of recommended operational and "
            "maintenance spares:",
            body_style,
        ),
        Spacer(1, 10),
        om_table,
    ]
    create_pdf(os.path.join(output_dir, "om_spares.pdf"), om_story)
    print("Generated om_spares.pdf")


if __name__ == "__main__":
    generate_mock_data()
