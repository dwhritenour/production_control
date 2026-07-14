"""
Work Order SO Packet PDF Generator - Anvil Server Module
=========================================================
Generates a bundled PDF packet for all work orders on a single sales order.

Layout:
  Page 1  — Cover / Summary page
            Sales order header, customer, due date, SO-level notes,
            table of all WOs in the packet with part#, qty, config status
  Page 2+ — Individual work order sheets (one per page)
            Same layout as the existing single-WO PDF, with SKU-level notes

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: March 2026
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
KeepTogether, HRFlowable, PageBreak
)

# Import centralized configuration and existing PDF generator
from . import db_config
from .work_order_pdf_generator import get_work_order_data, _now_eastern
from .work_order_dashboard import _current_actor, log_wo_cancel


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


# =============================================================================
# DATA RETRIEVAL
# =============================================================================

def get_so_work_orders(sales_order_number, sales_order_rel):
  """
  Fetch all work order IDs for a given sales order.

  Returns:
    list of dicts with work_order_id, work_order_number, part_number,
    quantity, wo_override_qty, status, sales_order_line, due_date
  """
  conn = get_connection()
  cursor = conn.cursor()

  cursor.execute("""
    SELECT
      wo.work_order_id,
      wo.work_order_number,
      wo.part_number,
      wo.sales_order_line,
      wo.quantity,
      wo.wo_override_qty,
      wo.status,
      wo.due_date,
      COALESCE(so.nx_attrib_val, wo.attribute_value) AS so_attrib_val,
      ad.nx_attribute_value AS standard_attrib_val
    FROM dbo.work_orders wo
    LEFT JOIN dbo.sales_orders_due so
      ON wo.sales_order_number = so.nx_order_numb
     AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
     AND wo.sales_order_rel = so.nx_rel_numb
    LEFT JOIN dbo.attrib_val_default ad
      ON wo.part_number = ad.nx_part_code
    WHERE wo.sales_order_number = %s
      AND wo.sales_order_rel = %s
    ORDER BY wo.sales_order_line ASC
  """, (sales_order_number, sales_order_rel))

  rows = []
  for row in cursor.fetchall():
    cleaned = {}
    for key, val in row.items():
      if val is None:
        cleaned[key] = ''
      elif isinstance(val, Decimal):
        cleaned[key] = float(val)
      elif isinstance(val, datetime):
        cleaned[key] = val
      elif isinstance(val, bytes):
        cleaned[key] = val.decode('utf-8', errors='replace')
      else:
        cleaned[key] = str(val).strip() if isinstance(val, str) else val
    rows.append(cleaned)

  conn.close()
  return rows


def get_so_header_info(sales_order_number, sales_order_rel):
  """
  Fetch sales-order-level info for the cover page.

  Returns:
    dict with so_level_notes, ship_by_date, or empty dict if not found.
  """
  conn = get_connection()
  cursor = conn.cursor()

  cursor.execute("""
    SELECT TOP 1
      so.nx_so_notes      AS so_level_notes,
      so.nx_dship_by_date AS ship_by_date
    FROM dbo.sales_orders_due so
    WHERE so.nx_order_numb = %s
      AND so.nx_rel_numb   = %s
  """, (sales_order_number, sales_order_rel))

  row = cursor.fetchone()
  conn.close()

  if not row:
    return {'so_level_notes': '', 'ship_by_date': ''}

  result = {}
  for key, val in row.items():
    if val is None:
      result[key] = ''
    elif isinstance(val, (datetime,)):
      result[key] = val
    elif isinstance(val, bytes):
      result[key] = val.decode('utf-8', errors='replace')
    else:
      result[key] = str(val).strip() if isinstance(val, str) else val

  return result


# =============================================================================
# PDF GENERATION — COVER PAGE ELEMENTS
# =============================================================================

def build_cover_page_elements(sales_order_number, sales_order_rel,
                              so_header, wo_list, styles_dict):
  """
  Build the ReportLab flowable elements for the cover/summary page.

  Args:
    sales_order_number: str
    sales_order_rel: str
    so_header: dict from get_so_header_info
    wo_list: list of dicts from get_so_work_orders
    styles_dict: dict of ParagraphStyle objects

  Returns:
    list of ReportLab flowable elements
  """
  s = styles_dict
  elements = []

  # --- Colors ---
  titan_blue = colors.Color(0.2, 0.3, 0.5)
  light_gray = colors.Color(0.93, 0.93, 0.93)
  medium_gray = colors.Color(0.7, 0.7, 0.7)
  alert_red = colors.Color(0.85, 0.15, 0.15)
  ok_green = colors.Color(0.2, 0.6, 0.2)

  # --- Header ---
  header_data = [[
    Paragraph("TITAN FLOW CONTROL", s['company']),
    Paragraph(f"SO {sales_order_number}-{sales_order_rel}", s['wo_number'])
  ]]
  header_table = Table(header_data, colWidths=[4 * inch, 3.3 * inch])
  header_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
  ]))
  elements.append(header_table)

  elements.append(Paragraph(
    "WORK ORDER PACKET — STRAINING ELEMENT DEPARTMENT",
    s['doc_title']
  ))

  elements.append(HRFlowable(
    width="100%", thickness=2, color=titan_blue,
    spaceBefore=2, spaceAfter=10
  ))

  # --- Packet summary info ---
  total_wo = len(wo_list)
  total_qty = sum(
    (int(wo.get('wo_override_qty') or 0) or int(wo.get('quantity') or 0))
    for wo in wo_list
  )

  ship_date = so_header.get('ship_by_date', '')
  if isinstance(ship_date, datetime):
    ship_date = ship_date.strftime('%m/%d/%Y')
  elif hasattr(ship_date, 'strftime'):
    ship_date = ship_date.strftime('%m/%d/%Y')

  # Count specials
  special_count = 0
  for wo in wo_list:
    std = str(wo.get('standard_attrib_val', '')).strip()
    ord_val = str(wo.get('so_attrib_val', '')).strip()
    if std and ord_val and std != ord_val:
      special_count += 1

  summary_data = [
    [Paragraph("<b>Sales Order:</b>", s['normal']),
     Paragraph(f"{sales_order_number}-{sales_order_rel}", s['bold']),
     Paragraph("<b>Ship By:</b>", s['normal']),
     Paragraph(str(ship_date) if ship_date else 'Not set', s['bold'])],
    [Paragraph("<b>Work Orders:</b>", s['normal']),
     Paragraph(str(total_wo), s['bold']),
     Paragraph("<b>Total Qty:</b>", s['normal']),
     Paragraph(f"{total_qty:,}", s['bold'])],
  ]

  if special_count > 0:
    summary_data.append([
      Paragraph("<b>Special Configs:</b>", s['normal']),
      Paragraph(f"<font color='red'><b>{special_count} of {total_wo}</b></font>",
                s['normal']),
      Paragraph("", s['normal']),
      Paragraph("", s['normal'])
    ])

  summary_table = Table(summary_data,
                        colWidths=[1.4 * inch, 2.0 * inch, 1.2 * inch, 2.7 * inch])
  summary_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ('TOPPADDING', (0, 0), (-1, -1), 4),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ('BACKGROUND', (0, 0), (-1, -1), light_gray),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
    ('LINEBELOW', (0, 0), (-1, -2), 0.5, medium_gray),
  ]))
  elements.append(summary_table)
  elements.append(Spacer(1, 12))

  # --- Sales Order Notes (apply to all SKUs) ---
  so_notes = str(so_header.get('so_level_notes', '')).strip()
  if so_notes:
    elements.append(Paragraph("Sales Order Notes", s['section']))
    elements.append(Paragraph(so_notes, s['normal']))
    elements.append(Spacer(1, 12))

  # --- Work Order line-item table ---
  elements.append(Paragraph("Work Orders in This Packet", s['section']))

  # Table header
  table_data = [[
    Paragraph("<b>Line</b>", s['normal']),
    Paragraph("<b>WO #</b>", s['normal']),
    Paragraph("<b>Part Number</b>", s['normal']),
    Paragraph("<b>Qty</b>", s['normal']),
    Paragraph("<b>Config</b>", s['normal']),
    Paragraph("<b>Status</b>", s['normal']),
  ]]

  for wo in wo_list:
    # Config indicator
    std = str(wo.get('standard_attrib_val', '')).strip()
    ord_val = str(wo.get('so_attrib_val', '')).strip()
    if not std or not ord_val:
      config_text = "N/A"
      config_color = 'gray'
    elif std == ord_val:
      config_text = "Standard"
      config_color = 'green'
    else:
      config_text = "SPECIAL"
      config_color = 'red'

    # Quantity - use override if set
    override = wo.get('wo_override_qty')
    if override and float(override) > 0:
      qty_str = str(int(override))
    else:
      qty_str = str(int(wo.get('quantity', 0))) if wo.get('quantity') else ''

    table_data.append([
      Paragraph(str(wo.get('sales_order_line', '')), s['normal']),
      Paragraph(str(wo.get('work_order_number', '')), s['bold']),
      Paragraph(str(wo.get('part_number', '')), s['normal']),
      Paragraph(qty_str, s['normal']),
      Paragraph(f"<font color='{config_color}'><b>{config_text}</b></font>",
                s['normal']),
      Paragraph(str(wo.get('status', '')), s['normal']),
    ])

  wo_table = Table(table_data,
                   colWidths=[0.5 * inch, 1.3 * inch, 1.5 * inch,
                              0.6 * inch, 1.0 * inch, 1.2 * inch])
  wo_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ('TOPPADDING', (0, 0), (-1, -1), 4),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.85, 0.9, 0.95)),
    ('LINEBELOW', (0, 0), (-1, 0), 1, titan_blue),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
    ('LINEBELOW', (0, 1), (-1, -2), 0.5, medium_gray),
    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_gray]),
  ]))
  elements.append(wo_table)
  elements.append(Spacer(1, 20))

  # --- Footer ---
  elements.append(Paragraph(
    f"Packet generated: {_now_eastern().strftime('%m/%d/%Y %I:%M %p')}  |  "
    f"Titan Flow Control — Testing Phase",
    s['small']
  ))

  return elements


# =============================================================================
# PDF GENERATION — INDIVIDUAL WO PAGE ELEMENTS
# =============================================================================

def build_wo_page_elements(data, page_num, total_pages, styles_dict):
  """
  Build the ReportLab flowable elements for a single work order page
  within the packet. Mirrors the existing single-WO PDF layout.

  Args:
    data: dict from get_work_order_data
    page_num: int — page number within the packet (for header reference)
    total_pages: int — total WO pages
    styles_dict: dict of ParagraphStyle objects

  Returns:
    list of ReportLab flowable elements (starts with PageBreak)
  """
  s = styles_dict
  elements = []

  # --- Colors ---
  titan_blue = colors.Color(0.2, 0.3, 0.5)
  light_gray = colors.Color(0.93, 0.93, 0.93)
  medium_gray = colors.Color(0.7, 0.7, 0.7)
  alert_red = colors.Color(0.85, 0.15, 0.15)
  ok_green = colors.Color(0.2, 0.6, 0.2)

  # Page break before each WO page
  elements.append(PageBreak())

  # --- Header row: Company name left, WO# right ---
  header_data = [[
    Paragraph("TITAN FLOW CONTROL", s['company']),
    Paragraph(data.get('work_order_number', ''), s['wo_number'])
  ]]
  header_table = Table(header_data, colWidths=[4 * inch, 3.3 * inch])
  header_table.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
  ]))
  elements.append(header_table)

  # Subtitle with packet page reference
  elements.append(Paragraph(
    f"WORK ORDER — STRAINING ELEMENT DEPARTMENT"
    f"&nbsp;&nbsp;&nbsp;({page_num} of {total_pages})",
    s['doc_title']
  ))

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
    is_special = None
  elif standard != ordered:
    config_text = "\u26a0  SPECIAL CONFIGURATION — NON-STANDARD STRAINING ELEMENT"
    is_special = True
  else:
    config_text = "\u2713  STANDARD CONFIGURATION"
    is_special = False

  if is_special:
    config_style = s['special']
    banner_bg = colors.Color(1.0, 0.93, 0.93)
  elif is_special is False:
    config_style = s['standard']
    banner_bg = colors.Color(0.93, 1.0, 0.93)
  else:
    config_style = s['bold']
    banner_bg = light_gray

  banner_data = [[Paragraph(config_text, config_style)]]
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
  elements.append(Paragraph("Order Details", s['section']))

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

  # Quantity display — use override if set
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
    [Paragraph("<b>Sales Order:</b>", s['normal']),
     Paragraph(f"{data.get('sales_order_number', '')}-{data.get('sales_order_rel') or '001'}  /  Line {data.get('sales_order_line', '')}", s['normal']),
     Paragraph("<b>Customer:</b>", s['normal']),
     Paragraph('', s['normal'])],
    [Paragraph("<b>Part Number:</b>", s['normal']),
     Paragraph(data.get('part_number', ''), s['normal']),
     Paragraph("<b>Description:</b>", s['normal']),
     Paragraph(data.get('part_sku', ''), s['normal'])],
    [Paragraph("<b>Quantity:</b>", s['normal']),
     Paragraph(qty_display, s['bold']),
     Paragraph("<b>WO Due Date:</b>", s['normal']),
     Paragraph(str(wo_due_date) if wo_due_date else 'Not Set', s['bold'])],
    [Paragraph("<b>Status:</b>", s['normal']),
     Paragraph(data.get('status', ''), s['normal']),
     Paragraph("<b>SO Due Date:</b>", s['normal']),
     Paragraph(str(due_date), s['normal'])],
    [Paragraph("<b>Carrier:</b>", s['normal']),
     Paragraph(data.get('carrier', ''), s['normal']),
     Paragraph("<b>Ship By:</b>", s['normal']),
     Paragraph(str(ship_date), s['normal'])],
    [Paragraph("<b>MOS:</b>", s['normal']),
     Paragraph(data.get('mos', ''), s['normal']),
     Paragraph('', s['normal']),
     Paragraph('', s['normal'])],
    [Paragraph("<b>SKU Weight:</b>", s['normal']),
     Paragraph(sku_weight_display, s['normal']),
     Paragraph("<b>Total Weight:</b>", s['bold']),
     Paragraph(total_weight_display, s['bold'])],
  ]

  detail_table = Table(detail_data,
                       colWidths=[1.2 * inch, 2.2 * inch, 1.2 * inch, 2.7 * inch])
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
  elements.append(Paragraph("Configuration Comparison", s['section']))

  std_parts = [p.strip() for p in standard.split('|')] if standard else ['', '']
  ord_parts = [p.strip() for p in ordered.split('|')] if ordered else ['', '']

  std_material = std_parts[0] if len(std_parts) > 0 else ''
  std_se = std_parts[1] if len(std_parts) > 1 else ''
  ord_material = ord_parts[0] if len(ord_parts) > 0 else ''
  ord_se = ord_parts[1] if len(ord_parts) > 1 else ''

  mat_match = (std_material == ord_material)
  se_match = (std_se == ord_se)

  config_data = [
    [Paragraph("", s['normal']),
     Paragraph("<b>Material</b>", s['bold']),
     Paragraph("<b>Straining Element</b>", s['bold'])],
    [Paragraph("<b>Standard:</b>", s['normal']),
     Paragraph(std_material or 'N/A', s['normal']),
     Paragraph(std_se or 'N/A', s['normal'])],
    [Paragraph("<b>Ordered:</b>", s['normal']),
     Paragraph(ord_material or 'N/A', s['bold']),
     Paragraph(ord_se or 'N/A', s['bold'])],
  ]

  config_table = Table(config_data,
                       colWidths=[1.2 * inch, 3.0 * inch, 3.1 * inch])

  config_style_cmds = [
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ('TOPPADDING', (0, 0), (-1, -1), 5),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ('BOX', (0, 0), (-1, -1), 1, medium_gray),
    ('LINEBELOW', (0, 0), (-1, 0), 1, titan_blue),
    ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.85, 0.9, 0.95)),
    ('LINEBELOW', (0, 1), (-1, 1), 0.5, medium_gray),
  ]

  if not mat_match:
    config_style_cmds.append(
      ('BACKGROUND', (1, 2), (1, 2), colors.Color(1.0, 0.9, 0.9)))
  if not se_match:
    config_style_cmds.append(
      ('BACKGROUND', (2, 2), (2, 2), colors.Color(1.0, 0.9, 0.9)))

  config_table.setStyle(TableStyle(config_style_cmds))
  elements.append(config_table)
  elements.append(Spacer(1, 10))

  # --- SKU-level notes (specific to this work order) ---
  so_notes = data.get('so_notes', '')
  if isinstance(so_notes, str):
    so_notes = so_notes.strip()
  else:
    so_notes = ''

  if so_notes:
    elements.append(Paragraph("SKU Notes", s['section']))
    elements.append(Paragraph(so_notes, s['normal']))
    elements.append(Spacer(1, 10))

  # --- Divider before signature ---
  elements.append(HRFlowable(
    width="100%", thickness=1, color=medium_gray,
    spaceBefore=10, spaceAfter=10
  ))

  # --- Signature / completion block ---
  elements.append(Paragraph("Completion", s['section']))

  sig_data = [
    [Paragraph("Completed By:", s['normal']), '',
     Paragraph("Date:", s['normal']), ''],
    [Paragraph("Quality Check:", s['normal']), '',
     Paragraph("Date:", s['normal']), ''],
  ]

  sig_table = Table(sig_data,
                    colWidths=[1.3 * inch, 2.5 * inch, 0.8 * inch, 2.7 * inch])
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
    s['small']
  ))

  return elements


# =============================================================================
# PDF GENERATION — MAIN ENTRY POINT
# =============================================================================

def generate_so_packet_pdf(sales_order_number, sales_order_rel):
  """
  Generate a multi-page PDF packet for all work orders on a sales order.

  Page 1: Cover / summary with SO-level notes and WO line-item table
  Pages 2+: Individual work order sheets (one per page)

  Args:
    sales_order_number: str
    sales_order_rel: str

  Returns:
    Anvil BlobMedia containing the PDF, or None if no WOs found.
  """
  # Fetch data
  wo_list = get_so_work_orders(sales_order_number, sales_order_rel)
  if not wo_list:
    return None

  so_header = get_so_header_info(sales_order_number, sales_order_rel)

  # Create PDF buffer
  buffer = io.BytesIO()

  # Define colors
  titan_blue = colors.Color(0.2, 0.3, 0.5)

  # Create document
  doc = SimpleDocTemplate(
    buffer,
    pagesize=letter,
    rightMargin=0.6 * inch,
    leftMargin=0.6 * inch,
    topMargin=0.5 * inch,
    bottomMargin=0.75 * inch
  )

  # Shared styles
  base_styles = getSampleStyleSheet()

  styles_dict = {
    'company': ParagraphStyle(
      'Company', parent=base_styles['Heading1'],
      fontSize=22, spaceAfter=2,
      textColor=titan_blue
    ),
    'doc_title': ParagraphStyle(
      'DocTitle', parent=base_styles['Heading2'],
      fontSize=16, spaceAfter=4,
      textColor=colors.black
    ),
    'wo_number': ParagraphStyle(
      'WONumber', parent=base_styles['Heading1'],
      fontSize=20, spaceBefore=0, spaceAfter=0,
      textColor=titan_blue, alignment=TA_RIGHT
    ),
    'section': ParagraphStyle(
      'SectionHeader', parent=base_styles['Heading3'],
      fontSize=12, spaceBefore=10, spaceAfter=4,
      textColor=titan_blue, bold=True
    ),
    'normal': ParagraphStyle(
      'NormalText', parent=base_styles['Normal'],
      fontSize=10, spaceAfter=2
    ),
    'bold': ParagraphStyle(
      'BoldText', parent=base_styles['Normal'],
      fontSize=10, spaceAfter=2,
      fontName='Helvetica-Bold'
    ),
    'special': ParagraphStyle(
      'SpecialAlert', parent=base_styles['Normal'],
      fontSize=14, spaceAfter=4,
      textColor=colors.Color(0.85, 0.15, 0.15),
      fontName='Helvetica-Bold', alignment=TA_CENTER
    ),
    'standard': ParagraphStyle(
      'StandardOK', parent=base_styles['Normal'],
      fontSize=14, spaceAfter=4,
      textColor=colors.Color(0.2, 0.6, 0.2),
      fontName='Helvetica-Bold', alignment=TA_CENTER
    ),
    'small': ParagraphStyle(
      'SmallText', parent=base_styles['Normal'],
      fontSize=8, textColor=colors.grey
    ),
  }

  # ---------------------------------------------------------------
  # Build all elements
  # ---------------------------------------------------------------
  elements = []

  # Cover page
  elements.extend(
    build_cover_page_elements(
      sales_order_number, sales_order_rel,
      so_header, wo_list, styles_dict
    )
  )

  # Individual WO pages
  total_pages = len(wo_list)
  for idx, wo_summary in enumerate(wo_list, start=1):
    wo_data = get_work_order_data(wo_summary['work_order_id'])
    if wo_data:
      elements.extend(
        build_wo_page_elements(wo_data, idx, total_pages, styles_dict)
      )

  # ---------------------------------------------------------------
  # Build PDF
  # ---------------------------------------------------------------
  doc.build(elements)

  pdf_content = buffer.getvalue()
  buffer.close()

  # Filename
  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  filename = f"SO_{sales_order_number}_{sales_order_rel}_Packet_{timestamp}.pdf"

  return anvil.BlobMedia('application/pdf', pdf_content, name=filename)


# =============================================================================
# ANVIL CALLABLE
# =============================================================================

@anvil.server.callable
def download_so_packet_pdf(sales_order_number, sales_order_rel):
  """
  Generate and return a bundled PDF packet for all work orders
  on a sales order.

  Called from client when 'Print SO Packet' is clicked.

  Args:
    sales_order_number: str
    sales_order_rel: str

  Returns:
    dict with status and either pdf_media or error message
  """
  try:
    pdf_media = generate_so_packet_pdf(
      sales_order_number, sales_order_rel
    )

    if pdf_media is None:
      return {
        'status': 'error',
        'message': 'No work orders found for this sales order'
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


# =============================================================================
# START ALL WORK ORDERS FOR A SALES ORDER
# =============================================================================

@anvil.server.callable
def start_so_work_orders(sales_order_number, sales_order_rel):
  """
  Move all 'Created' work orders for a sales order to 'In Progress'.

  Only transitions WOs currently in 'Created' status — already started
  or completed WOs are left untouched.

  Args:
    sales_order_number: str
    sales_order_rel: str

  Returns:
    dict with status, started count, and already-started count
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Count how many are eligible to start
    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status = 'Created'
    """, (sales_order_number, sales_order_rel))
    created_count = cursor.fetchone()['cnt']

    # Count how many are already past 'Created'
    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status != 'Created'
    """, (sales_order_number, sales_order_rel))
    already_started = cursor.fetchone()['cnt']

    if created_count == 0:
      conn.close()
      return {
        'status': 'success',
        'started': 0,
        'already_started': already_started,
        'message': 'No work orders in Created status to start'
      }

    # Bulk update
    cursor.execute("""
      UPDATE dbo.work_orders
      SET status = 'In Progress'
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status = 'Created'
    """, (sales_order_number, sales_order_rel))

    conn.commit()
    conn.close()

    return {
      'status': 'success',
      'started': created_count,
      'already_started': already_started,
      'message': f"Started {created_count} work order{'s' if created_count != 1 else ''}"
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# COMPLETE ALL WORK ORDERS FOR A SALES ORDER
# =============================================================================

@anvil.server.callable
def complete_so_work_orders(sales_order_number, sales_order_rel,
                            completed_by=None):
  """
  Mark all 'In Progress' work orders for a sales order as 'Complete'.

  Only transitions WOs currently in 'In Progress' status — Created,
  Verify, or already Complete WOs are left untouched.

  Args:
    sales_order_number: str
    sales_order_rel: str
    completed_by: str — who completed them (defaults to 'Production')

  Returns:
    dict with status, completed count, and skipped count
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Count how many are eligible to complete
    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status = 'In Progress'
    """, (sales_order_number, sales_order_rel))
    eligible_count = cursor.fetchone()['cnt']

    # Count how many are not eligible (Created, Complete, Verify, etc.)
    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status != 'In Progress'
    """, (sales_order_number, sales_order_rel))
    skipped_count = cursor.fetchone()['cnt']

    if eligible_count == 0:
      conn.close()
      return {
        'status': 'success',
        'completed': 0,
        'skipped': skipped_count,
        'message': 'No work orders in In Progress status to complete'
      }

    # Bulk update
    cursor.execute("""
      UPDATE dbo.work_orders
      SET status = 'Complete',
          completed_at = GETDATE(),
          completed_by = %s
      WHERE sales_order_number = %s
        AND sales_order_rel = %s
        AND status = 'In Progress'
    """, (completed_by or 'Production', sales_order_number, sales_order_rel))

    conn.commit()
    conn.close()

    return {
      'status': 'success',
      'completed': eligible_count,
      'skipped': skipped_count,
      'message': f"Completed {eligible_count} work order{'s' if eligible_count != 1 else ''}"
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }




# =============================================================================
# RELEASE ALL WORK ORDERS FOR A SALES ORDER  (SE Department rollout 2026-06-17)
# =============================================================================

@anvil.server.callable
def release_so_work_orders(sales_order_number, sales_order_rel):
  """
  Release every 'Created' work order for a sales order to 'Released' so the
  whole SO packet appears on the SE Department floor board at once.

  Only transitions WOs currently in 'Created' status; anything already
  Released or further along is left untouched.

  Returns:
    dict with status, released count, already_released count, message
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status = 'Created'
    """, (sales_order_number, sales_order_rel))
    created_count = cursor.fetchone()['cnt']

    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status != 'Created'
    """, (sales_order_number, sales_order_rel))
    already_released = cursor.fetchone()['cnt']

    if created_count == 0:
      conn.close()
      return {
        'status': 'success',
        'released': 0,
        'already_released': already_released,
        'message': 'No work orders in Created status to release'
      }

    cursor.execute("""
      UPDATE dbo.work_orders
      SET status = 'Released'
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status = 'Created'
    """, (sales_order_number, sales_order_rel))

    conn.commit()
    conn.close()
    return {
      'status': 'success',
      'released': created_count,
      'already_released': already_released,
      'message': f"Released {created_count} work order{'s' if created_count != 1 else ''}"
    }

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# UN-RELEASE ALL WORK ORDERS FOR A SALES ORDER  (added 2026-06-23)
# =============================================================================

@anvil.server.callable
def unrelease_so_work_orders(sales_order_number, sales_order_rel):
  """
  Reverse a packet Release: move every 'Released' work order for a sales order
  back to 'Created', pulling the whole packet off the SE Department board.

  Only transitions WOs currently in 'Released'. WOs the floor has already
  picked up ('Assigned' / 'In Progress' / 'Paused') are left untouched and
  reported as skipped; Created / terminal WOs are ignored. The status guard
  lives in the WHERE clause so a stale client cannot un-release floor work.

  Returns:
    dict with status, unreleased count, skipped_on_floor count, message
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status = 'Released'
    """, (sales_order_number, sales_order_rel))
    released_count = cursor.fetchone()['cnt']

    cursor.execute("""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status IN ('Assigned', 'In Progress', 'Paused')
    """, (sales_order_number, sales_order_rel))
    skipped_on_floor = cursor.fetchone()['cnt']

    if released_count == 0:
      conn.close()
      return {
        'status': 'success',
        'unreleased': 0,
        'skipped_on_floor': skipped_on_floor,
        'message': 'No work orders in Released status to un-release'
      }

    cursor.execute("""
      UPDATE dbo.work_orders
      SET status = 'Created'
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status = 'Released'
    """, (sales_order_number, sales_order_rel))

    conn.commit()
    conn.close()
    return {
      'status': 'success',
      'unreleased': released_count,
      'skipped_on_floor': skipped_on_floor,
      'message': f"Un-released {released_count} work order{'s' if released_count != 1 else ''}"
    }

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# CANCEL ALL WORK ORDERS FOR A SALES ORDER  (SE Department rollout 2026-06-17)
# =============================================================================

# Live statuses the office may cancel in bulk. Terminal/external statuses
# (Complete, Cancelled, the three '- Verify' states) are left untouched.
_CANCELLABLE_STATUSES = ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')


@anvil.server.callable
def get_so_cancellable_work_orders(sales_order_number, sales_order_rel):
  """Preview the live work orders a Cancel SO Packet would cancel, so the
  confirmation can list them before anything changes. Uses the same status
  filter as cancel_so_work_orders (_CANCELLABLE_STATUSES)."""
  try:
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ", ".join(["%s"] * len(_CANCELLABLE_STATUSES))
    cursor.execute(f"""
      SELECT work_order_number, sales_order_line, status
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status IN ({placeholders})
      ORDER BY sales_order_line
    """, (sales_order_number, sales_order_rel) + _CANCELLABLE_STATUSES)
    wos = [{'work_order_number': r['work_order_number'],
            'sales_order_line': r['sales_order_line'],
            'status': r['status']} for r in cursor.fetchall()]
    conn.close()
    return {'status': 'success', 'work_orders': wos}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


@anvil.server.callable
def cancel_so_work_orders(sales_order_number, sales_order_rel):
  """
  Cancel every live work order for a sales order (status -> 'Cancelled').

  Cancels WOs in any of Created / Released / Assigned / In Progress / Paused.
  Already-Complete, already-Cancelled, and the e21 '- Verify' WOs are skipped.

  Returns:
    dict with status, cancelled count, skipped count, message
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    placeholders = ", ".join(["%s"] * len(_CANCELLABLE_STATUSES))

    cursor.execute(f"""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status IN ({placeholders})
    """, (sales_order_number, sales_order_rel) + _CANCELLABLE_STATUSES)
    eligible_count = cursor.fetchone()['cnt']

    cursor.execute(f"""
      SELECT COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status NOT IN ({placeholders})
    """, (sales_order_number, sales_order_rel) + _CANCELLABLE_STATUSES)
    skipped_count = cursor.fetchone()['cnt']

    if eligible_count == 0:
      conn.close()
      return {
        'status': 'success',
        'cancelled': 0,
        'skipped': skipped_count,
        'message': 'No live work orders to cancel'
      }

    # Capture the live WOs (for the audit) before flipping them.
    cursor.execute(f"""
      SELECT work_order_id, work_order_number, status
      FROM dbo.work_orders
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status IN ({placeholders})
    """, (sales_order_number, sales_order_rel) + _CANCELLABLE_STATUSES)
    _victims = cursor.fetchall()

    cursor.execute(f"""
      UPDATE dbo.work_orders
      SET status = 'Cancelled'
      WHERE sales_order_number = %s AND sales_order_rel = %s
        AND status IN ({placeholders})
    """, (sales_order_number, sales_order_rel) + _CANCELLABLE_STATUSES)

    # Audit each packet cancel (best-effort; never block the cancel).
    try:
      _actor = _current_actor()
      for _v in _victims:
        log_wo_cancel(cursor, _v['work_order_id'], _v['work_order_number'],
                      sales_order_number, sales_order_rel, _v['status'],
                      'packet', _actor)
    except Exception:
      pass

    conn.commit()
    conn.close()
    return {
      'status': 'success',
      'cancelled': eligible_count,
      'skipped': skipped_count,
      'message': f"Cancelled {eligible_count} work order{'s' if eligible_count != 1 else ''}"
    }

  except Exception as e:
    return {'status': 'error', 'message': str(e)}