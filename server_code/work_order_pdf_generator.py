"""
Work Order PDF Generator - Anvil Server Module
================================================
Generates printable work order sheets for the Screen Department.
Uses ReportLab for PDF generation.

Features:
- Company header with work order number
- Sales order and part details
- Standard vs. ordered configuration comparison
- Special configuration callout
- Sales order notes from e21
- Work order notes
- Signature/completion block

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: February 2026
"""

import anvil.server
import anvil.media
from datetime import datetime
from decimal import Decimal
import io

# ReportLab imports
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
KeepTogether, HRFlowable
)

# Import centralized configuration
from . import db_config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def _now_eastern():
  """Current wall-clock time in US Eastern (EST/EDT) for printout display.
  Anvil servers run in UTC, so a bare datetime.now() prints UTC; convert to
  America/New_York (DST-aware) so timestamps match our local time."""
  from datetime import timezone, timedelta
  utc_now = datetime.now(timezone.utc)
  try:
    from zoneinfo import ZoneInfo
    return utc_now.astimezone(ZoneInfo("America/New_York"))
  except Exception:
    # Fallback (no tz database): US Eastern DST rules -- 2nd Sunday of March
    # to 1st Sunday of November.
    import calendar
    from datetime import date
    y = utc_now.year
    def _nth_sunday(month, nth):
      suns = [wk[calendar.SUNDAY] for wk in calendar.monthcalendar(y, month)
              if wk[calendar.SUNDAY] != 0]
      return suns[nth - 1]
    dst_start = date(y, 3, _nth_sunday(3, 2))
    dst_end = date(y, 11, _nth_sunday(11, 1))
    is_dst = dst_start <= utc_now.date() < dst_end
    return utc_now.astimezone(timezone(timedelta(hours=-4 if is_dst else -5)))


# =============================================================================
# DATA RETRIEVAL
# =============================================================================

def get_work_order_data(work_order_id):
  """
    Fetch all data needed for a work order PDF.
    
    Returns:
        dict with work order details, or None if not found.
  """
  conn = get_connection()
  cursor = conn.cursor()

  cursor.execute("""
    SELECT
        wo.work_order_id,
        wo.work_order_number,
        wo.sales_order_number,
        wo.sales_order_rel,
        wo.sales_order_line,
        wo.part_number,
        wo.part_attribute,
        wo.attribute_value,
        wo.attribute_desc,
        wo.quantity,
        wo.wo_override_qty,
        wo.wo_due_date,
        wo.notes,
        wo.due_date,
        wo.status,
        wo.created_at,
        wo.printed_at,
        wo.created_by,
        so.nx_notes         AS so_notes,
        so.nx_so_notes      AS so_level_notes, 
        so.nx_attrib_val    AS so_attrib_val,
        so.nx_dship_by_date AS so_ship_by_date,
        so.nx_sku           AS part_sku,
        so.nx_sku_desc      AS part_desc,
        so.nx_carrier       AS carrier,
        so.nx_mos           AS mos,
        ps.act_weight_lb    AS sku_weight,
        so.nx_qty_ordered   AS so_qty,
        ad.nx_attribute_value  AS standard_attrib_val,
        ad.nx_attribute_desc   AS standard_attrib_desc
    FROM dbo.work_orders wo
    LEFT JOIN dbo.sales_orders_due so
        ON wo.sales_order_number = so.nx_order_numb
       AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
       AND wo.sales_order_rel = so.nx_rel_numb    -- ADD THIS
    LEFT JOIN dbo.attrib_val_default ad
        ON wo.part_number = ad.nx_part_code
    LEFT JOIN dbo.part_specs ps
        ON wo.part_number = ps.sku
    WHERE wo.work_order_id = %s
""", (work_order_id,))

  row = cursor.fetchone()
  conn.close()

  if not row:
    return None

  # Clean up values
  data = {}
  for key, val in row.items():
    if val is None:
      data[key] = ''
    elif isinstance(val, (datetime,)):
      data[key] = val
    elif isinstance(val, Decimal):
      data[key] = float(val)
    elif isinstance(val, bytes):
      data[key] = val.decode('utf-8', errors='replace')
    else:
      data[key] = str(val).strip() if isinstance(val, str) else val

  return data


# =============================================================================
# PDF GENERATION
# =============================================================================

def generate_work_order_pdf(work_order_id):
  """
    Generate a PDF work order sheet.
    
    Args:
        work_order_id: int - the work order to generate
    
    Returns:
        Anvil BlobMedia containing the PDF, or None if not found.
  """
  data = get_work_order_data(work_order_id)
  if not data:
    return None

  # Create PDF buffer
  buffer = io.BytesIO()

  # Define colors
  titan_blue = colors.Color(0.2, 0.3, 0.5)
  light_gray = colors.Color(0.93, 0.93, 0.93)
  medium_gray = colors.Color(0.7, 0.7, 0.7)
  alert_red = colors.Color(0.85, 0.15, 0.15)
  ok_green = colors.Color(0.2, 0.6, 0.2)

  # Create document
  doc = SimpleDocTemplate(
    buffer,
    pagesize=letter,
    rightMargin=0.6 * inch,
    leftMargin=0.6 * inch,
    topMargin=0.5 * inch,
    bottomMargin=0.75 * inch
  )

  # Styles
  styles = getSampleStyleSheet()

  style_company = ParagraphStyle(
    'Company', parent=styles['Heading1'],
    fontSize=22, spaceAfter=2,
    textColor=titan_blue
  )
  style_doc_title = ParagraphStyle(
    'DocTitle', parent=styles['Heading2'],
    fontSize=16, spaceAfter=4,
    textColor=colors.black
  )
  style_wo_number = ParagraphStyle(
    'WONumber', parent=styles['Heading1'],
    fontSize=20, spaceBefore=0, spaceAfter=0,
    textColor=titan_blue, alignment=TA_RIGHT
  )
  style_section = ParagraphStyle(
    'SectionHeader', parent=styles['Heading3'],
    fontSize=12, spaceBefore=10, spaceAfter=4,
    textColor=titan_blue, bold=True
  )
  style_normal = ParagraphStyle(
    'NormalText', parent=styles['Normal'],
    fontSize=10, spaceAfter=2
  )
  style_bold = ParagraphStyle(
    'BoldText', parent=styles['Normal'],
    fontSize=10, spaceAfter=2,
    fontName='Helvetica-Bold'
  )
  style_special = ParagraphStyle(
    'SpecialAlert', parent=styles['Normal'],
    fontSize=14, spaceAfter=4,
    textColor=alert_red, fontName='Helvetica-Bold',
    alignment=TA_CENTER
  )
  style_standard = ParagraphStyle(
    'StandardOK', parent=styles['Normal'],
    fontSize=14, spaceAfter=4,
    textColor=ok_green, fontName='Helvetica-Bold',
    alignment=TA_CENTER
  )
  style_small = ParagraphStyle(
    'SmallText', parent=styles['Normal'],
    fontSize=8, textColor=colors.grey
  )
  style_printed = ParagraphStyle(
    'PrintedStamp', parent=styles['Normal'],
    fontSize=9, spaceBefore=2, spaceAfter=0,
    textColor=medium_gray, alignment=TA_RIGHT
  )

  # ---------------------------------------------------------------
  # Build elements
  # ---------------------------------------------------------------
  elements = []

  # --- Header row: Company name left, WO# + printed stamp right ---
  printed_stamp = _now_eastern().strftime('%m/%d/%Y %I:%M %p')
  header_data = [[
    Paragraph("TITAN FLOW CONTROL", style_company),
    [Paragraph(data.get('work_order_number', ''), style_wo_number),
     Paragraph(f"Printed: {printed_stamp}", style_printed)]
  ]]
  header_table = Table(header_data, colWidths=[4 * inch, 3.3 * inch])
  header_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
  ]))
  elements.append(header_table)

  # Subtitle
  elements.append(Paragraph("WORK ORDER — STRAINING ELEMENT DEPARTMENT", style_doc_title))

  # Divider
  elements.append(HRFlowable(
    width="100%", thickness=2, color=titan_blue,
    spaceBefore=2, spaceAfter=10
  ))

  # --- Configuration alert banner ---
  standard = data.get('standard_attrib_val', '').strip()
  ordered = data.get('so_attrib_val', '').strip()

  if not standard or not ordered:
    config_text = "CONFIGURATION COMPARISON UNAVAILABLE"
    config_style = style_bold
    is_special = None
  elif standard != ordered:
    config_text = "\u26a0  SPECIAL CONFIGURATION — NON-STANDARD STRAINING ELEMENT"
    config_style = style_special
    is_special = True
  else:
    config_text = "\u2713  STANDARD CONFIGURATION"
    config_style = style_standard
    is_special = False

  # Banner with background
  banner_data = [[Paragraph(config_text, config_style)]]
  if is_special:
    banner_bg = colors.Color(1.0, 0.93, 0.93)  # Light red
  elif is_special is False:
    banner_bg = colors.Color(0.93, 1.0, 0.93)  # Light green
  else:
    banner_bg = light_gray

  banner_table = Table(banner_data, colWidths=[7.3 * inch])
  banner_table.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, -1), banner_bg),
    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ('TOPPADDING', (0, 0), (-1, -1), 8),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
  ]))
  elements.append(banner_table)
  elements.append(Spacer(1, 10))

  # --- Order details table ---
  elements.append(Paragraph("Order Details", style_section))

  due_date = data.get('due_date', '')
  if isinstance(due_date, datetime):
    due_date = due_date.strftime('%m/%d/%Y')
  elif hasattr(due_date, 'strftime'):
    due_date = due_date.strftime('%m/%d/%Y')

  ship_date = data.get('so_ship_by_date', '')
  if isinstance(ship_date, datetime):
    ship_date = ship_date.strftime('%m/%d/%Y')
  elif hasattr(ship_date, 'strftime'):
    ship_date = ship_date.strftime('%m/%d/%Y')

  # WO Due Date (production completion target)
  wo_due_date = data.get('wo_due_date', '')
  if isinstance(wo_due_date, datetime):
    wo_due_date = wo_due_date.strftime('%m/%d/%Y')
  elif hasattr(wo_due_date, 'strftime'):
    wo_due_date = wo_due_date.strftime('%m/%d/%Y')

  # Quantity display — use override if set, otherwise e21 quantity
  e21_qty = str(data.get('quantity', ''))
  override_qty = data.get('wo_override_qty')
  if override_qty and float(override_qty) > 0:
    qty_display = f"{int(override_qty)}  (e21: {e21_qty})"
  else:
    qty_display = e21_qty

  # SKU weight + total. Effective qty = override when set, else SO qty. Weight
  # comes from part_specs.act_weight_lb; blank ("—") when none is on file.
  try:
    _w = float(data.get('sku_weight') or 0)
  except (ValueError, TypeError):
    _w = 0.0
  try:
    _oq = float(override_qty) if override_qty not in (None, '') else 0
  except (ValueError, TypeError):
    _oq = 0
  try:
    _q = float(data.get('quantity') or 0)
  except (ValueError, TypeError):
    _q = 0
  _eff_qty = _oq if _oq > 0 else _q
  if _w > 0:
    sku_weight_display = f"{_w:g} lb"
    total_weight_display = f"{_w * _eff_qty:g} lb"
  else:
    sku_weight_display = '—'
    total_weight_display = '—' 

  detail_data = [
    [Paragraph("<b>Sales Order:</b>", style_normal),
     Paragraph(f"{data.get('sales_order_number', '')}-{data.get('sales_order_rel') or '001'}  /  Line {data.get('sales_order_line', '')}", style_normal),
     Paragraph("<b>Customer:</b>", style_normal),
     Paragraph('', style_normal)],
    [Paragraph("<b>Part Number:</b>", style_normal),
     Paragraph(data.get('part_number', ''), style_normal),
     Paragraph("<b>Description:</b>", style_normal),
     Paragraph(data.get('part_desc', '') or data.get('part_sku', ''), style_normal)],
    [Paragraph("<b>Quantity:</b>", style_normal),
     Paragraph(qty_display, style_bold),
     Paragraph("<b>WO Due Date:</b>", style_normal),
     Paragraph(str(wo_due_date) if wo_due_date else 'Not Set', style_bold)],
    [Paragraph("<b>Status:</b>", style_normal),
     Paragraph(data.get('status', ''), style_normal),
     Paragraph("<b>SO Due Date:</b>", style_normal),
     Paragraph(str(due_date), style_normal)],
    [Paragraph("<b>Carrier:</b>", style_normal),
     Paragraph(data.get('carrier', ''), style_normal),
     Paragraph("<b>Ship By:</b>", style_normal),
     Paragraph(str(ship_date), style_normal)],
    [Paragraph("<b>MOS:</b>", style_normal),
     Paragraph(data.get('mos', ''), style_normal),
     Paragraph('', style_normal),
     Paragraph('', style_normal)],
    [Paragraph("<b>SKU Weight:</b>", style_normal),
     Paragraph(sku_weight_display, style_normal),
     Paragraph("<b>Total Weight:</b>", style_bold),
     Paragraph(total_weight_display, style_bold)],
  ]

  detail_table = Table(detail_data, colWidths=[1.2 * inch, 2.2 * inch, 1.2 * inch, 2.7 * inch])
  detail_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ('TOPPADDING', (0, 0), (-1, -1), 3),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ('BACKGROUND', (0, 0), (-1, -1), light_gray),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
    ('LINEBELOW', (0, 0), (-1, -2), 0.5, medium_gray),
  ]))
  elements.append(detail_table)
  elements.append(Spacer(1, 10))

  # --- Configuration comparison ---
  elements.append(Paragraph("Configuration Comparison", style_section))

  # Split values for display
  std_parts = [p.strip() for p in standard.split('|')] if standard else ['', '']
  ord_parts = [p.strip() for p in ordered.split('|')] if ordered else ['', '']

  std_material = std_parts[0] if len(std_parts) > 0 else ''
  std_se = std_parts[1] if len(std_parts) > 1 else ''
  ord_material = ord_parts[0] if len(ord_parts) > 0 else ''
  ord_se = ord_parts[1] if len(ord_parts) > 1 else ''

  # Highlight differences
  mat_match = (std_material == ord_material)
  se_match = (std_se == ord_se)

  config_data = [
    [Paragraph("", style_normal),
     Paragraph("<b>Material</b>", style_bold),
     Paragraph("<b>Straining Element</b>", style_bold)],
    [Paragraph("<b>Standard:</b>", style_normal),
     Paragraph(std_material or 'N/A', style_normal),
     Paragraph(std_se or 'N/A', style_normal)],
    [Paragraph("<b>Ordered:</b>", style_normal),
     Paragraph(ord_material or 'N/A', style_bold),
     Paragraph(ord_se or 'N/A', style_bold)],
  ]

  config_table = Table(config_data, colWidths=[1.2 * inch, 3.0 * inch, 3.1 * inch])

  config_style_cmds = [
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ('TOPPADDING', (0, 0), (-1, -1), 5),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
    ('LINEBELOW', (0, 0), (-1, 0), 1, titan_blue),
    ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.85, 0.9, 0.95)),
    ('LINEBELOW', (0, 1), (-1, 1), 0.5, medium_gray),
  ]

  # Highlight non-matching cells in the Ordered row
  if not mat_match:
    config_style_cmds.append(('BACKGROUND', (1, 2), (1, 2), colors.Color(1.0, 0.9, 0.9)))
  if not se_match:
    config_style_cmds.append(('BACKGROUND', (2, 2), (2, 2), colors.Color(1.0, 0.9, 0.9)))

  config_table.setStyle(TableStyle(config_style_cmds))
  elements.append(config_table)
  elements.append(Spacer(1, 10))

  # --- Notes section ---
  so_notes = data.get('so_notes', '').strip()

  if so_notes:
    elements.append(Paragraph("Notes", style_section))
    elements.append(Paragraph(f"<b>SKU Notes:</b> {so_notes}", style_normal))
    elements.append(Spacer(1, 10))

  # --- Divider before signature ---
  elements.append(HRFlowable(
    width="100%", thickness=1, color=medium_gray,
    spaceBefore=10, spaceAfter=10
  ))

  # --- Signature / completion block ---
  elements.append(Paragraph("Completion", style_section))

  sig_data = [
    [Paragraph("Completed By:", style_normal), '',
     Paragraph("Date:", style_normal), ''],
    [Paragraph("Quality Check:", style_normal), '',
     Paragraph("Date:", style_normal), ''],
  ]

  sig_table = Table(sig_data, colWidths=[1.3 * inch, 2.5 * inch, 0.8 * inch, 2.7 * inch])
  sig_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ('TOPPADDING', (0, 0), (-1, -1), 12),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ('LINEBELOW', (1, 0), (1, 0), 1, colors.black),
    ('LINEBELOW', (3, 0), (3, 0), 1, colors.black),
    ('LINEBELOW', (1, 1), (1, 1), 1, colors.black),
    ('LINEBELOW', (3, 1), (3, 1), 1, colors.black),
  ]))
  elements.append(sig_table)

  # --- Footer ---
  elements.append(Spacer(1, 20))
  created_at = data.get('created_at', '')
  if isinstance(created_at, datetime):
    created_at = created_at.strftime('%m/%d/%Y %I:%M %p')
  elements.append(Paragraph(
    f"Generated: {_now_eastern().strftime('%m/%d/%Y %I:%M %p')}  |  "
    f"Created: {created_at}  |  "
    f"Titan Flow Control — Testing Phase",
    style_small
  ))

  # ---------------------------------------------------------------
  # Build PDF
  # ---------------------------------------------------------------
  doc.build(elements)

  pdf_content = buffer.getvalue()
  buffer.close()

  # Filename
  wo_num = data.get('work_order_number', 'WO').replace('-', '_')
  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  filename = f"{wo_num}_{timestamp}.pdf"

  return anvil.BlobMedia('application/pdf', pdf_content, name=filename)


# =============================================================================
# ANVIL CALLABLE
# =============================================================================

@anvil.server.callable
def download_work_order_pdf(work_order_id):
  """
    Generate and return a work order PDF.
    Called from client when btn_print is clicked.
    
    Args:
        work_order_id: int - the work order to print
    
    Returns:
        dict with status and either pdf_media or error message
  """
  try:
    pdf_media = generate_work_order_pdf(work_order_id)

    if pdf_media is None:
      return {
        'status': 'error',
        'message': 'Work order not found'
      }

    return {
      'status': 'success',
      'pdf_media': pdf_media,
      'filename': pdf_media.name
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }