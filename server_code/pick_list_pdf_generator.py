# =============================================================================
# pick_list_pdf_generator.py
# -----------------------------------------------------------------------------
# Version:       5.18.0
# Last updated:  2026-06-10
# Changelog (this module only):
#   5.18.0 (2026-06-10) One-line companion change to the v5.18 zero-Hub-
#                       stock feature in transfer_truck_optimizer.py:
#                       added 'Cancelled' to _PICK_LIST_BLOCKED_STATUSES.
#                       'Cancelled' is the new terminal status assigned by
#                       remove_assignment_cancel_rec when the Hub Manager
#                       clears an unfulfillable SKU (qty 0 in trip_editor).
#                       The normal flow deletes the tta row before the rec
#                       is cancelled, so a Cancelled rec should never reach
#                       a pick slip — this is defense-in-depth, same
#                       rationale as the v5.13 inner guard.
#   5.13.0 (2026-05-26) Defense-in-depth status guard: move the v5.12
#                       block-list check into generate_pick_list_pdf
#                       itself, so EVERY caller is protected — not just
#                       the download_pick_list_pdf reprint wrapper.
#                       Motivated by 2026-05-26 incident: rec 1099
#                       (YS59I0300) was Paused throughout the morning,
#                       yet appeared on a freshly-printed picking slip
#                       at 12:24, then was Obsoleted at 11:41 UTC by
#                       the demand-resolved cleanup, and finally was
#                       absent from the trip editor when the Hub crew
#                       went to ship the 29 units they had already
#                       picked. Whichever code path put a Paused rec
#                       on the PDF, the renderer itself should refuse.
#                       Behavior:
#                         * Blocked recs (Obsolete, Rejected, Paused)
#                           are FILTERED OUT before fetch_pick_list_data
#                           runs.
#                         * Each filtered rec is logged to ai_agent_log
#                           with action_type 'PickList_Blocked_Rec_
#                           Filtered' for passive monitoring.
#                         * If filtering removes every rec, the
#                           function returns None (no PDF rendered).
#                         * Otherwise, the slip renders without the
#                           blocked SKUs but still includes all valid
#                           ones — graceful degradation rather than
#                           hard failure (which is what the v5.12
#                           wrapper still does for the reprint UX,
#                           preserved unchanged).
#                       The v5.12 download_pick_list_pdf wrapper is
#                       NOT touched. It still does its preflight check
#                       and returns the structured error response for
#                       the reprint UI. The new inner guard is a
#                       backstop for paths the wrapper doesn't cover.
#                       Does NOT solve the race where a rec briefly
#                       transitions Approved -> Applied -> rolled-back
#                       inside the apply_transfers window — at PDF
#                       time the rec would be Applied (not in the
#                       block list). That race is a separate
#                       investigation (see project memory on the
#                       Hub Manager workflow drift item).
#   5.12.0 (2026-05-04) Status guard in download_pick_list_pdf: refuse to
#                       render a PDF when ANY selected rec is at status
#                       Obsolete, Rejected, or Paused. Returns a clear
#                       error response listing the blocked rec_ids and
#                       SKUs so the caller can fix their selection.
#                       Motivated by 2026-05-04 incident: rec 731
#                       (YS58D0800) was Obsoleted by v5.9 dedupe AFTER
#                       being included in a pick-list selection, and the
#                       PDF rendered anyway because the generator did
#                       no status validation. Hub Manager almost shipped
#                       24 units against a rec that no longer existed.
#                       Companion fix in transfer_truck_optimizer.py
#                       (v5.12 dedupe filter) and transfer_ai_review.py
#                       (v5.12 stale qty=0 cleanup).
#   5.8.4 (2026-04-29) FIRST VERSIONED RELEASE — brought into the v5.8+
#                       versioning scheme.
#                       BUG FIX: Hub Manager qty edits made in trip_editor
#                       (which write to transfer_trip_assignments.adjusted_qty)
#                       were silently ignored by the printed pick list,
#                       which only read recommendation-level qty fields.
#                       Fix:
#                         - LEFT JOIN transfer_trip_assignments on
#                           recommendation_id and select tta.adjusted_qty.
#                         - Insert adjusted_qty as the highest-priority
#                           entry in the qty fallback (ahead of
#                           manager_override_qty / ai_recommended_qty /
#                           calculated_transfer_qty).
#                       Companion fix in driver_delivery.py v5.8.4.
# =============================================================================

"""
Pick List PDF Generator - Anvil Server Module
==============================================
Generates a Picking Slip PDF when the Hub Manager clicks btn_apply on
the transfer_rec form. This is a temporary replacement for the legacy
CSV export that was being sent to TGI while we wait for the e21 tie-in.

Layout is modeled on the sample 'Picking Slip' (Pick_List_Sample.pdf)
while matching the visual style of the Transfer Trip Assignment Sheet
produced in trip_pdf_generator.py (Helvetica family, Titan blue accent,
light-grey header rows, subtle grid borders, and a timestamped footer).

Data sources:
    dbo.ai_transfer_recommendations  - SKU, event_type, time_urgency, qty
    dbo.transfer_trip_assignments    - tta.adjusted_qty (trip-level edit)
    dbo.part_specs                   - part_desc, act_weight_lb, weight_factor
    dbo.hub_product_locations        - primary location_code (bin location)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: April 2026
"""

__version__       = "5.18.0"
__version_date__  = "2026-06-10"
__version_notes__ = (
  "Added 'Cancelled' to _PICK_LIST_BLOCKED_STATUSES (companion to the "
  "v5.18 remove_assignment_cancel_rec zero-Hub-stock feature in "
  "transfer_truck_optimizer.py). Defense-in-depth — the normal flow "
  "deletes the assignment before cancelling the rec."
)

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
SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, KeepTogether
)

# Import centralized configuration
from . import db_config
from .db_config import TRANSFER_CONSTANTS


# =============================================================================
# STYLE CONSTANTS  (kept consistent with trip_pdf_generator.py)
# =============================================================================

TITAN_BLUE = colors.Color(0.2, 0.3, 0.5)          # Primary brand color
TITAN_BLUE_LIGHT = colors.Color(0.9, 0.9, 0.95)   # Header row / section bg
TABLE_HEADER_BG = colors.Color(0.3, 0.4, 0.5)     # Table header strip
SUBTOTAL_BG = colors.Color(0.95, 0.95, 0.95)      # Subtotal row


# =============================================================================
# HELPERS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def format_number(value, decimals=0):
  """Format number with commas."""
  if value is None:
    return "0"
  if isinstance(value, Decimal):
    value = float(value)
  if decimals == 0:
    return "{:,.0f}".format(value)
  return "{:,.{d}f}".format(value, d=decimals)


# =============================================================================
# v5.12 / v5.13 STATUS GUARD
# =============================================================================

# Statuses that should never appear on a printed pick list. These are the
# terminal-or-on-hold states: Obsolete (cleaned up by mark_stale or v5.9
# dedupe), Rejected (Hub Manager said no), Paused (Hub Manager wants more
# time to think), Cancelled (v5.18: Hub Manager cleared an unfulfillable
# SKU — no Hub stock — via qty-0 in trip_editor). Anything else (Pending,
# Approved, Applied, Auto-Approved, Trip_Assigned, Finalized, Delivered)
# is allowed through.
_PICK_LIST_BLOCKED_STATUSES = ('Obsolete', 'Rejected', 'Paused', 'Cancelled')


def _check_blocked_pick_list_recs(conn, recommendation_ids):
  """v5.12: Return a list of blocking-rec dicts, one per rec in the
  selection whose current status is in _PICK_LIST_BLOCKED_STATUSES.

  Empty list means "all clear, render the PDF." Anything else means
  the caller should either refuse (download_pick_list_pdf, for the
  reprint UX) or filter + log (generate_pick_list_pdf, v5.13.0 inner
  guard, for the main render path).

  Implementation note: queries r.status fresh each time, so a rec
  that was Approved when the user opened the pick-list dialog and
  Obsoleted between then and clicking Print will still be caught.
  Critical for the YS58D0800 incident pattern (rec 731 Obsoleted by
  v5.9 dedupe AFTER being added to a pick-list selection) and the
  YS59I0300 / rec 1099 incident on 2026-05-26 (Paused rec ended up
  on a fresh slip via a path the v5.12 wrapper didn't cover).
  """
  if not recommendation_ids:
    return []

  cursor = conn.cursor()
  placeholders = ','.join(['%s'] * len(recommendation_ids))
  status_placeholders = ','.join(['%s'] * len(_PICK_LIST_BLOCKED_STATUSES))
  cursor.execute(
    f"""
        SELECT recommendation_id, sku, status
        FROM   dbo.ai_transfer_recommendations
        WHERE  recommendation_id IN ({placeholders})
          AND  status IN ({status_placeholders})
        ORDER BY recommendation_id
    """,
    tuple(recommendation_ids) + _PICK_LIST_BLOCKED_STATUSES
  )
  return [
    {
      'recommendation_id': row['recommendation_id'],
      'sku': row['sku'],
      'status': row['status'],
    }
    for row in cursor.fetchall()
  ]


def _log_blocked_pick_list_recs(conn, blocked):
  """v5.13.0: Audit log for blocked-but-filtered recs.

  Inserts one ai_agent_log row per blocked rec the inner guard
  filtered out of a PDF render. The audit trail lets us passively
  monitor how often this defense fires — frequent hits point to an
  upstream bug where blocked recs are being passed to the PDF
  generator (the YS59I0300 incident is the canonical example).

  No-op when 'blocked' is empty. Commits the inserts so the rows
  survive even if the subsequent rendering raises.
  """
  if not blocked:
    return

  cursor = conn.cursor()
  for b in blocked:
    cursor.execute(
      """
        INSERT INTO dbo.ai_agent_log (
            action_type, sku, old_value, new_value, reason,
            action_timestamp, executed_by
        )
        VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
      """,
      (
        'PickList_Blocked_Rec_Filtered',
        b['sku'],
        f"Status:{b['status']}",
        'Filtered from PDF',
        (f"v5.13.0 inner guard: rec {b['recommendation_id']} in blocked "
         f"status '{b['status']}' was filtered out of pick-list PDF "
         "generation. Investigate the caller — a blocked rec shouldn't "
         "have reached the renderer."),
        'pick_list_pdf_generator',
      )
    )
  conn.commit()


# =============================================================================
# DATA RETRIEVAL
# =============================================================================

def fetch_pick_list_data(conn, recommendation_ids):
  """
    Pull everything needed to render the Picking Slip for the given
    recommendation IDs.

    Returns:
        dict with keys:
            batch_id      - single batch id string (or '')
            generated_on  - datetime when pick list is produced
            items         - list of dicts (one per SKU line)
            total_weight  - sum of (qty * unit_weight_lb) across items
    """
  cursor = conn.cursor()

  if not recommendation_ids:
    return {
      'batch_id': '',
      'generated_on': datetime.now(),
      'items': [],
      'total_weight': 0.0,
    }

  placeholders = ','.join(['%s'] * len(recommendation_ids))

  # 1) Pull the recommendation rows. Precedence (highest first):
  #    tta.adjusted_qty (Hub Manager edit in trip_editor)
  #      > r.manager_override_qty (older recommendation-level override)
  #      > r.ai_recommended_qty
  #      > r.calculated_transfer_qty
  #
  #    The LEFT JOIN to transfer_trip_assignments is what makes the
  #    Hub Manager's trip-level qty edits visible to the picking slip.
  #    Prior to v5.8.4 this query only read from ai_transfer_recommendations,
  #    so manager edits made in the trip_editor (which write to
  #    tta.adjusted_qty) were silently ignored on the printed pick list.
  cursor.execute(f"""
        SELECT
            r.recommendation_id,
            r.sku,
            r.event_type,
            r.time_urgency,
            tta.adjusted_qty,
            r.manager_override_qty,
            r.ai_recommended_qty,
            r.calculated_transfer_qty,
            r.batch_id
        FROM dbo.ai_transfer_recommendations r
        LEFT JOIN dbo.transfer_trip_assignments tta
            ON tta.recommendation_id = r.recommendation_id
        WHERE r.recommendation_id IN ({placeholders})
        ORDER BY r.event_type DESC, r.sku
    """, tuple(recommendation_ids))
  recs = list(cursor.fetchall())

  if not recs:
    return {
      'batch_id': '',
      'generated_on': datetime.now(),
      'items': [],
      'total_weight': 0.0,
    }

  skus = list({row['sku'] for row in recs})
  sku_placeholders = ','.join(['%s'] * len(skus))

  # 2) part_specs: description + unit weight
  cursor.execute(f"""
        SELECT
            sku,
            part_desc,
            COALESCE(act_weight_lb, 0) AS act_weight_lb,
            COALESCE(weight_factor, 1) AS weight_factor
        FROM dbo.part_specs
        WHERE sku IN ({sku_placeholders})
    """, tuple(skus))
  part_specs = {}
  for row in cursor.fetchall():
    part_specs[row['sku']] = {
      'part_desc': row['part_desc'] or row['sku'],
      'unit_weight_lb': float(row['act_weight_lb']) * float(row['weight_factor']),
    }

  # 3) Hub primary location per SKU (may be missing; we'll fall back to '')
  try:
    cursor.execute(f"""
            SELECT sku, location_code
            FROM dbo.hub_product_locations
            WHERE sku IN ({sku_placeholders})
              AND primary_location = 1
        """, tuple(skus))
    locations = {row['sku']: row['location_code'] for row in cursor.fetchall()}
  except Exception:
    # Table may not exist in every environment; don't let that block PDF
    locations = {}

  # 4) Build line items
  items = []
  total_weight = 0.0
  for rec in recs:
    sku = rec['sku']
    # v5.8.4: trip-level adjusted_qty wins over recommendation-level fields
    qty = int(rec.get('adjusted_qty')
              or rec['manager_override_qty']
              or rec['ai_recommended_qty']
              or rec['calculated_transfer_qty']
              or 0)
    if qty <= 0:
      continue

    spec = part_specs.get(sku, {'part_desc': sku, 'unit_weight_lb': 0.0})
    line_weight = qty * spec['unit_weight_lb']
    total_weight += line_weight

    items.append({
      'sku': sku,
      'part_desc': spec['part_desc'],
      'event_type': f"Type {rec['event_type']}" if rec['event_type'] else '',
      'time_urgency': rec['time_urgency'] or '',
      'uom': TRANSFER_CONSTANTS['uom'],        # 'EA'
      'location': locations.get(sku, ''),
      'qty': qty,
      'line_weight': line_weight,
    })

  batch_ids = {rec['batch_id'] for rec in recs if rec['batch_id']}
  batch_id = batch_ids.pop() if len(batch_ids) == 1 else (
    ', '.join(sorted(batch_ids)) if batch_ids else ''
  )

  return {
    'batch_id': batch_id,
    'generated_on': datetime.now(),
    'items': items,
    'total_weight': total_weight,
  }


# =============================================================================
# PDF SECTIONS
# =============================================================================

def build_top_banner(styles, data):
  """
    Two-column top banner:
      Left  - Titan Flow Control address block
      Right - 'PICKING SLIP' title + BATCH ID / ORDER NO / PAGE fields
    """
  # LEFT: company info
  company_name_style = ParagraphStyle(
    'CompanyName', parent=styles['Normal'],
    fontName='Helvetica-Bold', fontSize=11, leading=13,
    textColor=TITAN_BLUE,
  )
  company_addr_style = ParagraphStyle(
    'CompanyAddr', parent=styles['Normal'],
    fontName='Helvetica-Bold', fontSize=9, leading=11,
  )

  left_flowables = [
    Paragraph("TITAN FLOW CONTROL", company_name_style),
    Paragraph("290 CORPORATE DRIVE", company_addr_style),
    Paragraph("LUMBERTON, NC  28358", company_addr_style),
    Paragraph("Tel: 910-735-0000", company_addr_style),
    Paragraph("Fax: 910-738-3848", company_addr_style),
  ]

  # RIGHT: title + batch info (two-column mini-table)
  title_style = ParagraphStyle(
    'PickTitle', parent=styles['Normal'],
    fontName='Helvetica-Bold', fontSize=20, alignment=TA_RIGHT,
    textColor=colors.black, spaceAfter=6,
  )
  right_flowables = [
    Paragraph("PICKING SLIP", title_style),
    Spacer(1, 4),
  ]

  info_data = [
    ['BATCH ID:', data['batch_id'] or '—'],
    ['PAGE:', 'Page 1 of 1'],
  ]
  info_tbl = Table(info_data, colWidths=[0.9 * inch, 1.9 * inch])
  info_tbl.setStyle(TableStyle([
    ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
    ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
    ('FONTSIZE', (0, 0), (-1, -1), 9),
    ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
    ('ALIGN', (1, 0), (1, -1), 'LEFT'),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ('TOPPADDING', (0, 0), (-1, -1), 1),
  ]))
  right_flowables.append(info_tbl)

  banner = Table(
    [[left_flowables, right_flowables]],
    colWidths=[4.0 * inch, 3.5 * inch],
  )
  banner.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
  ]))
  return banner


def build_ship_order_block(styles):
  """Two-column SHIP TO / ORDER FROM block."""
  label_style = ParagraphStyle(
    'AddrLabel', parent=styles['Normal'],
    fontName='Helvetica-Bold', fontSize=10, leading=12,
  )
  addr_style = ParagraphStyle(
    'Addr', parent=styles['Normal'],
    fontName='Helvetica', fontSize=9, leading=11,
  )

  ship_to = [
    Paragraph("<b>SHIP TO:</b> STARLITE FACILITY", label_style),
    Paragraph("1401 STARLITE DRIVE", addr_style),
    Paragraph("LUMBERTON, NC  28358", addr_style),
  ]
  order_from = [
    Paragraph("<b>ORDER FROM:</b> THE HUB", label_style),
    Paragraph("797 CATON ROAD", addr_style),
    Paragraph("LUMBERTON, NC  28360", addr_style),
  ]

  block = Table(
    [[ship_to, order_from]],
    colWidths=[3.75 * inch, 3.75 * inch],
  )
  block.setStyle(TableStyle([
    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ('TOPPADDING', (0, 0), (-1, -1), 0),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
  ]))
  return block


def build_summary_row(data):
  """NUMBER OF SKUs | GENERATED ON | MOS summary strip."""
  gen_str = data['generated_on'].strftime('%Y-%m-%d %H:%M:%S')
  summary = [
    ['NUMBER OF SKUs', 'GENERATED ON', 'MOS'],
    [str(len(data['items'])), gen_str, 'TITAN TRUCK'],
  ]
  tbl = Table(summary, colWidths=[1.6 * inch, 3.4 * inch, 2.5 * inch])
  tbl.setStyle(TableStyle([
    # Label row
    ('BACKGROUND', (0, 0), (-1, 0), TITAN_BLUE_LIGHT),
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 9),

    # Value row
    ('FONTNAME', (0, 1), (-1, 1), 'Helvetica'),
    ('FONTSIZE', (0, 1), (-1, 1), 9),

    # General
    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ('LEFTPADDING', (0, 0), (-1, -1), 5),
    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ('TOPPADDING', (0, 0), (-1, -1), 3),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
  ]))
  return tbl


def build_items_table(data):
  """
    Main picking table:
      LINE | PRODUCT CODE / PART DESCRIPTION | TIME URGENCY | UM
           | LOCATION | TRANSFER QTY

    EVENT TYPE was removed from the rendered table per Dave's request.
    The 0.80 inch freed by dropping that column is redistributed to
    the four remaining content columns (TIME URGENCY, UM, LOCATION,
    TRANSFER QTY). Total table width stays 7.50 inch so the page
    layout and signature block below are unchanged.
    """
  header = [
    'LINE',
    'PRODUCT CODE / PART DESCRIPTION',
    'TIME URGENCY',
    'UM',
    'LOCATION',
    'TRANSFER QTY',
  ]
  rows = [header]

  for i, item in enumerate(data['items'], start=1):
    desc = f"{item['sku']} / {item['part_desc']}"
    rows.append([
      str(i),
      desc,
      item['time_urgency'],
      item['uom'],
      item['location'] or '',
      format_number(item['qty'], 2),
    ])

  # Old (7 cols): 0.45 + 2.80 + 0.80 + 0.95 + 0.40 + 0.95 + 1.15 = 7.50 in
  # New (6 cols): 0.45 + 2.80 + 1.15 + 0.55 + 1.20 + 1.35        = 7.50 in
  #   TIME URGENCY  0.95 -> 1.15 (+0.20)  'Critical'/'High' no longer tight
  #   UM            0.40 -> 0.55 (+0.15)  header 'UM' fits on one line
  #   LOCATION      0.95 -> 1.20 (+0.25)  bin codes like 'A-01-05' breathe
  #   TRANSFER QTY  1.15 -> 1.35 (+0.20)  comfortable right-aligned numbers
  col_widths = [
    0.45 * inch,  # LINE
    2.80 * inch,  # PRODUCT CODE / PART DESC
    1.15 * inch,  # TIME URGENCY
    0.55 * inch,  # UM
    1.20 * inch,  # LOCATION
    1.35 * inch,  # TRANSFER QTY
  ]

  tbl = Table(rows, colWidths=col_widths, repeatRows=1)
  tbl.setStyle(TableStyle([
    # Header
    ('BACKGROUND', (0, 0), (-1, 0), TABLE_HEADER_BG),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 9),
    ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
    ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
    ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
    ('TOPPADDING', (0, 0), (-1, 0), 6),

    # Data rows
    ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
    ('FONTSIZE', (0, 1), (-1, -1), 9),
    ('ALIGN', (0, 1), (0, -1), 'CENTER'),     # LINE centered
    ('ALIGN', (2, 1), (4, -1), 'CENTER'),     # TIME URGENCY..LOCATION centered
    ('ALIGN', (5, 1), (5, -1), 'RIGHT'),      # TRANSFER QTY right
    ('VALIGN', (0, 1), (-1, -1), 'TOP'),
    ('TOPPADDING', (0, 1), (-1, -1), 4),
    ('BOTTOMPADDING', (0, 1), (-1, -1), 4),

    # Grid
    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ('LINEBELOW', (0, 0), (-1, 0), 1.5, TABLE_HEADER_BG),
  ]))
  return tbl


def build_signature_block(total_weight):
  """
    Bottom strip: PICKED BY / INSPECT BY / PACKED BY / WT.
    Uses an empty cell with a bottom rule as the signature line,
    rather than a run of underscores (which didn't align cleanly
    at different font widths).
    """
  row = [
    ['PICKED BY', '',
     'INSPECT BY', '',
     'PACKED BY', '',
     'WT.', format_number(total_weight, 2)],
  ]
  tbl = Table(row, colWidths=[
    0.7 * inch, 1.4 * inch,
    0.8 * inch, 1.4 * inch,
    0.8 * inch, 1.4 * inch,
    0.4 * inch, 0.8 * inch,
  ])
  tbl.setStyle(TableStyle([
    ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, -1), 9),
    ('FONTNAME', (7, 0), (7, 0), 'Helvetica'),
    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ('ALIGN', (7, 0), (7, 0), 'RIGHT'),
    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
    ('TOPPADDING', (0, 0), (-1, -1), 10),
    ('BOTTOMPADDING', (0, 0), (-1, -1), 2),

    # Draw signature lines under the three blank cells only
    ('LINEBELOW', (1, 0), (1, 0), 0.75, colors.black),
    ('LINEBELOW', (3, 0), (3, 0), 0.75, colors.black),
    ('LINEBELOW', (5, 0), (5, 0), 0.75, colors.black),
  ]))
  return tbl


# =============================================================================
# PAGE FOOTER (matches trip_pdf_generator)
# =============================================================================

def add_page_number(canvas, doc):
  """Footer: 'Titan Flow Control' | 'Page N' | timestamp."""
  canvas.saveState()
  canvas.setFont('Helvetica', 9)
  canvas.setFillColor(colors.grey)
  canvas.drawString(0.75 * inch, 0.5 * inch, "Titan Flow Control")
  canvas.drawCentredString(letter[0] / 2, 0.5 * inch,
                           f"Page {canvas.getPageNumber()}")
  canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch,
                         datetime.now().strftime('%Y-%m-%d %H:%M'))
  canvas.restoreState()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def generate_pick_list_pdf(recommendation_ids):
  """
    Build a Picking Slip PDF for the given recommendation IDs.

    Args:
        recommendation_ids: list of recommendation_id values that were
            just transitioned to 'Applied' by apply_transfers().

    Returns:
        anvil.BlobMedia containing the PDF, or None if there are no
        qualifying line items (either nothing was passed in, or every
        rec was filtered out by the v5.13 status guard).

    v5.13.0: Defense-in-depth status guard. Before rendering, any rec
    whose CURRENT status is in _PICK_LIST_BLOCKED_STATUSES (Obsolete,
    Rejected, Paused) is filtered out and audit-logged to
    ai_agent_log. Remaining valid recs still render. If filtering
    removes every rec, returns None.

    This guard mirrors the v5.12 check that already lives in
    download_pick_list_pdf (the reprint wrapper) but covers EVERY
    call path — apply_transfers, future direct callers, anything.

    Does NOT solve the race where a rec briefly transitions
    Approved -> Applied -> rolled-back, because at PDF time the rec
    would be Applied (not in the block list). That's a separate
    investigation tied to the Hub Manager workflow drift item.
    """
  if not recommendation_ids:
    return None

  conn = get_connection()
  try:
    # v5.13.0: inner status guard. Filter blocked recs out of the
    # render list and log each one for passive monitoring. The
    # ai_agent_log entries are how we'll learn whether this defense
    # ever fires in production, and which upstream caller fed the
    # blocked rec in.
    blocked = _check_blocked_pick_list_recs(conn, recommendation_ids)
    if blocked:
      _log_blocked_pick_list_recs(conn, blocked)
      blocked_ids = {b['recommendation_id'] for b in blocked}
      recommendation_ids = [rid for rid in recommendation_ids
                            if rid not in blocked_ids]
      if not recommendation_ids:
        # Every rec was blocked; nothing left to render.
        return None

    data = fetch_pick_list_data(conn, recommendation_ids)
  finally:
    conn.close()

  if not data['items']:
    return None

  buffer = io.BytesIO()
  doc = SimpleDocTemplate(
    buffer,
    pagesize=letter,
    rightMargin=0.5 * inch,
    leftMargin=0.5 * inch,
    topMargin=0.5 * inch,
    bottomMargin=0.75 * inch,
  )
  styles = getSampleStyleSheet()

  elements = []
  elements.append(build_top_banner(styles, data))
  elements.append(Spacer(1, 10))
  elements.append(build_ship_order_block(styles))
  elements.append(Spacer(1, 12))
  elements.append(build_summary_row(data))
  elements.append(Spacer(1, 4))
  elements.append(build_items_table(data))
  elements.append(Spacer(1, 18))
  elements.append(build_signature_block(data['total_weight']))

  doc.build(elements,
            onFirstPage=add_page_number,
            onLaterPages=add_page_number)

  pdf_bytes = buffer.getvalue()
  buffer.close()

  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  batch_tag = (data['batch_id'] or 'nobatch').replace(',', '_').replace(' ', '')
  filename = f"Pick_List_{batch_tag}_{timestamp}.pdf"

  return anvil.BlobMedia('application/pdf', pdf_bytes, name=filename)



# =============================================================================
# OPTIONAL: standalone callable (handy for re-printing a prior batch)
# =============================================================================

@anvil.server.callable
def download_pick_list_pdf(recommendation_ids):
  """
    Wrapper so the client can re-print a pick list for an arbitrary set
    of recommendation IDs without re-running apply_transfers().

    v5.12: refuses to render if any selected rec is at status Obsolete,
    Rejected, or Paused. Returns
        {'status': 'error', 'reason': 'blocked_recs',
         'message': '...', 'blocked_recs': [{'recommendation_id': N,
         'sku': '...', 'status': '...'}, ...]}
    so the caller's UI can highlight which lines to remove from the
    selection (instead of silently producing a stale PDF — see
    YS58D0800/rec 731 incident on 2026-05-04).

    v5.13.0 NOTE: generate_pick_list_pdf now ALSO does this check
    internally (filter + log rather than hard-fail). This wrapper's
    preflight is kept as-is to preserve the structured error UX for
    the reprint screen — users get to see which lines are blocked
    before any render happens, instead of just getting a partial
    PDF and quiet audit-log entries.
    """
  try:
    # v5.12: status preflight before anything is rendered. Open a short-
    # lived connection just for the check; generate_pick_list_pdf opens
    # its own. Cheap because the SELECT is one round-trip.
    conn = get_connection()
    try:
      blocked = _check_blocked_pick_list_recs(conn, recommendation_ids)
    finally:
      conn.close()

    if blocked:
      summary = ', '.join(
        f"rec {b['recommendation_id']} ({b['sku']}, {b['status']})"
        for b in blocked
      )
      return {
        'status':  'error',
        'reason':  'blocked_recs',
        'message': (
          f"Cannot generate pick list: {len(blocked)} rec(s) are in a "
          f"non-printable status (Obsolete, Rejected, or Paused). "
          f"Remove from selection: {summary}"
        ),
        'blocked_recs': blocked,
      }

    pdf_media = generate_pick_list_pdf(recommendation_ids)
    if pdf_media is None:
      return {'status': 'error', 'message': 'No items to include on pick list.'}
    return {
      'status': 'success',
      'pdf_media': pdf_media,
      'filename': pdf_media.name,
    }
  except Exception as e:
    return {'status': 'error', 'message': str(e)}

