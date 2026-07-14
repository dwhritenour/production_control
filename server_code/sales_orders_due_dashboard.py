"""
Anvil Server Module - Sales Orders Due Dashboard
================================================
Server-side code for the Titan Flow Control Sales Orders Due form.

This module provides database connectivity and read-only logic for:
    - Fetching open sales order lines with view-mode and predicate filters
    - Lightweight summary counts for the header cards
    - Single-line detail lookup with full notes, inventory snapshot,
      related work order, and backorder flag
    - Excel export of the currently filtered view

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

This is a READ-ONLY module. No callable in this file modifies any e21 data.

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: May 2026
"""

# =============================================================================
# Version:       5.21.0
# Last updated:  2026-06-19
# Changelog (this module only):
#   5.21.0 (2026-06-19) Method-of-Shipment + Rush support (PC request).
#                       - Grid query now selects nx_carrier and nx_mos and
#                         exposes them on each line as 'nx_carrier'/'nx_mos'
#                         (trimmed). Drives two new grid columns.
#                       - New is_rush flag per line, computed from the
#                         RUSH_SHIP_METHODS whitelist (carrier+method matched
#                         together). Drives the Order # lightning indicator.
#                       - New show_rush_only filter param on
#                         get_sales_orders_due (and passed through the Excel
#                         export). AND-combines with all other filters.
#                       - New 'rush' summary count in both the grid-load
#                         counts and get_sales_orders_due_stats.
#                       Requires form v5.18.0+ (cb_rush_only, lk_rush_count)
#                       and row template v5.17.0+ (lightning glyph).
#                       Excel export column LAYOUT is intentionally unchanged
#                       (PC's v5.17.0 15-column spec); only the rush filter
#                       is honored. Adding Carrier/MOS to the sheet is a
#                       separate PC decision.
#   5.20.0 (2026-05-19) Excel export: added 'Created' column (header_
#                       create_date) right before 'Ship By'. Width 15,
#                       right-aligned. Also restored the export-function
#                       tail (Summary sheet + BlobMedia serialize +
#                       return + except) that had been silently truncated
#                       in the deployed v5.19.0 file.
#   5.19.0 (2026-05-19) Added 'created_on_date' filter parameter. When
#                       set, filters lines to those whose nx_hdate_entered
#                       falls on the specified calendar date. Drives the
#                       new dp_created_on DatePicker on the form. AND-
#                       combines with all other filters (view-mode, status,
#                       search, allocated-only, etc.). Passed through from
#                       the Excel export so downloads honor the filter.
#   5.18.0 (2026-05-19) Added 'header_create_date' field (nx_hdate_entered)
#                       to the grid query. Surfaces the SO header creation
#                       date so the shop floor can compare it against the
#                       ship-by date and spot newly-created orders with
#                       already-tight ship deadlines. Grid-only change;
#                       the v5.17.0 Excel export layout (PC's spec) is
#                       intentionally left untouched.
#   5.17.0 (2026-05-19) Excel export revamp — reshape the "Open Lines"
#                       sheet to the column set + widths + alignments
#                       Production Control supplied via the
#                       2026-05-19 sample export.
#                       Columns reduced from 23 to 15. Removed:
#                         Description, Header Ship By, Header Due,
#                         Diverged, Stale Ship-By, WO Status,
#                         Cost Center, Part Status.
#                       Remaining columns (in order):
#                         Days, Status, Order #, Item, SKU, Ship-To,
#                         Qty Ord, Qty Bal Due, Qty Committed,
#                         Qty Allocated, Ship By, Due, Starlite OH,
#                         Hub OH, WO #.
#                       Per-column widths set on Days(10), Status(15),
#                       Order #(15), Item(10), SKU(25), Ship-To(30),
#                       Qty Ord(18), Ship By(15), WO #(20). All other
#                       columns at Excel default width per spec.
#                       Per-column horizontal alignment: Days right,
#                       Status center, Ship By right, Due right; all
#                       other columns default (text-left, number-right).
#                       All data cells vertical-aligned center.
#                       Days-column conditional fill (red/amber/yellow/
#                       green by lateness) preserved.
#                       Summary sheet content unchanged (still includes
#                       Diverged and Stale Ship-By count rows).
#                       Grid query, line-detail callable, summary count
#                       query, and ship_by_stale flag — all unchanged
#                       from v5.15.0.
#   5.15.0 (2026-05-19) ship_by_stale flag (mixed-time-position:
#                       ship-by past, due future). New line field, new
#                       summary count, new filter param, new Excel
#                       column (now removed in 5.17.0 from the table,
#                       still present in the Summary sheet).
#   5.14.2 (2026-05-04) Removed has_backorder from grid query and Excel
#                       export (release suffix in Order # column made
#                       it redundant). Popup detail still computes it.
#   5.14.1 (2026-05-04) BUG FIX: has_backorder was per-SKU, lighting up
#                       on every line that shared a SKU with any
#                       backorder. Joined sales_backorders by full
#                       line key.
#   5.14.0 (2026-05-04) Added hub_on_hand to grid query and Excel
#                       export. Split inventory join into inv_st +
#                       inv_hub.
#   5.13.0 (2026-05-04) NEW MODULE — Sales Orders Due dashboard.
# =============================================================================

__version__       = "5.21.0"
__version_date__  = "2026-06-19"
__version_notes__ = (
  "Method-of-Shipment + Rush: nx_carrier/nx_mos grid columns, is_rush flag, "
  "show_rush_only filter, and a 'rush' summary count."
)


import anvil.server
import anvil.media
import re
from datetime import datetime, date
from decimal import Decimal
from io import BytesIO

# Import centralized configuration
from . import db_config


# =============================================================================
# RUSH SHIP-METHOD WHITELIST (v5.21.0)
# =============================================================================
# (carrier_code, method_of_shipment) pairs that Production Control treats as
# "rush". Supplied by PC on 2026-06-19. Carrier and method are matched
# TOGETHER on purpose: the same method code (e.g. '2DAY') counts as rush only
# for certain carriers, so a method-only test would over-flag.
#
# These are a fixed internal whitelist (no user input ever reaches them), so
# they are inlined into the SQL as a literal IN-list rather than parameterized.
# To add or remove a rush method, edit this list only — every query below
# derives its predicate from it.
RUSH_SHIP_METHODS = [
  ('DHL',  'EXP'),    ('DHL',  'EXPW'),   ('DHL',  'SAT'),
  ('ESTE', '2DAY'),   ('ESTE', 'NXTDAY'),
  ('FDXF', '2DAY'),   ('FDXF', 'NXTDAY'),
  ('FDXP', '2DAY'),   ('FDXP', 'NXTDAY'),
  ('FEDX', '2DAY'),   ('FEDX', '2DAYAM'), ('FEDX', 'NXTAM'),
  ('FEDX', 'NXTDAY'), ('FEDX', 'NXTSAV'), ('FEDX', 'SATDEL'),
  ('UPS',  '2DAY'),   ('UPS',  '2DAYAM'), ('UPS',  '3DAY'),
  ('UPS',  'NXTAM'),  ('UPS',  'NXTDAY'), ('UPS',  'NXTSAV'),
  ('UPS',  'S2DAY'),  ('UPS',  'SATDEL'), ('UPS',  'SNXAM'),
  ('UPS',  'SNXDAY'),
  ('UPSF', '2DAY'),   ('UPSF', 'NXTDAY'),
]

# 'CARRIER|METHOD' keys + a ready-to-inline SQL literal IN-list.
_RUSH_KEYS    = ["{}|{}".format(c, m) for (c, m) in RUSH_SHIP_METHODS]
_RUSH_IN_LIST = ", ".join("'" + k.replace("'", "''") + "'" for k in _RUSH_KEYS)


def _rush_bool_sql(prefix):
  """SQL boolean: TRUE when this row's carrier+method is a rush combination.

  `prefix` is the table alias plus dot — 'sod.' for the grid query, '' for
  the alias-less counts query. Carrier and method are trimmed and upper-cased
  before matching so space-padded CHAR values and case differences from e21
  don't cause misses. A NULL carrier or method concatenates to a key that
  cannot appear in the whitelist, so such rows are correctly non-rush.
  """
  expr = ("CONCAT(UPPER(RTRIM(LTRIM({p}nx_carrier))), '|', "
          "UPPER(RTRIM(LTRIM({p}nx_mos))))").format(p=prefix)
  return "{expr} IN ({inlist})".format(expr=expr, inlist=_RUSH_IN_LIST)


def _rush_case_sql(prefix):
  """1/0 CASE wrapper around _rush_bool_sql, for SELECT and SUM() use."""
  return "CASE WHEN {b} THEN 1 ELSE 0 END".format(b=_rush_bool_sql(prefix))


# openpyxl rejects XML-illegal control chars (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F).
# Strip them from any string that's about to land in a cell.
_EXCEL_ILLEGAL_CHARS = re.compile(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]')

def _excel_safe(v):
  if isinstance(v, str):
    return _EXCEL_ILLEGAL_CHARS.sub('', v)
  return v


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================

def make_serializable(value):
  """Convert non-serializable types so Anvil can ship them to the client."""
  if value is None:
    return None
  if isinstance(value, (datetime, date)):
    return value.isoformat()
  if isinstance(value, Decimal):
    return float(value)
  if isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  return value


def serialize_row(row):
  return {key: make_serializable(val) for key, val in row.items()}


# =============================================================================
# DATABASE CONNECTION
# =============================================================================

def get_connection():
  """Create and return a Nexus connection via centralized config."""
  return db_config.get_nexus_connection()


# =============================================================================
# DATE / FORMATTING HELPERS
# =============================================================================

def _fmt_date(iso_string):
  """ISO date/datetime string -> 'MM/DD/YYYY' for grid display."""
  if not iso_string:
    return ''
  try:
    return datetime.fromisoformat(iso_string).strftime('%m/%d/%Y')
  except (ValueError, TypeError):
    return iso_string


def _truncate(text, length=40):
  """Notes preview helper. Collapses CR/LF to spaces, then truncates."""
  if not text:
    return ''
  flat = ' '.join(text.replace('\r', ' ').replace('\n', ' ').split())
  if len(flat) <= length:
    return flat
  return flat[:length] + '…'


# =============================================================================
# VIEW MODE -> SQL PREDICATE
# =============================================================================

def _view_mode_predicate(view_mode, custom_start=None, custom_end=None):
  """Return (sql_fragment, params_list) for the given view-mode."""
  today = "CAST(GETDATE() AS DATE)"

  if view_mode == 'Late Only':
    return f" AND sod.nx_dship_by_date < {today}", []
  if view_mode == 'Due Today':
    return f" AND sod.nx_dship_by_date = {today}", []
  if view_mode == 'Next 30 Days':
    return (f" AND sod.nx_dship_by_date BETWEEN {today} "
            f"AND DATEADD(day, 30, {today})"), []
  if view_mode == 'All Open':
    return "", []
  if view_mode == 'Custom Range':
    if custom_start and custom_end:
      return " AND sod.nx_dship_by_date BETWEEN %s AND %s", [custom_start, custom_end]
    return f" AND sod.nx_dship_by_date <= DATEADD(day, 7, {today})", []

  return f" AND sod.nx_dship_by_date <= DATEADD(day, 7, {today})", []


# =============================================================================
# MAIN GRID QUERY
# =============================================================================

@anvil.server.callable
def get_sales_orders_due(view_mode='Late + Next 7 Days',
                         status_filter=None,
                         search_text=None,
                         has_allocated_only=False,
                         show_diverged_only=False,
                         show_ship_by_stale_only=False,
                         show_rush_only=False,
                         created_on_date=None,
                         custom_start=None,
                         custom_end=None):
  """Fetch open sales order lines with optional filtering. Argument
  semantics unchanged from v5.15.0, plus:
    show_rush_only (v5.21.0) — when True, restrict to lines whose
      carrier+method is in the RUSH_SHIP_METHODS whitelist. AND-combines
      with every other filter."""
  try:
    conn = get_connection()
    cursor = conn.cursor()

    date_pred, date_params = _view_mode_predicate(
      view_mode, custom_start, custom_end
    )
    rush_case = _rush_case_sql('sod.')

    query = f"""
        SELECT
            sod.nx_order_numb,
            sod.nx_rel_numb,
            sod.nx_item_no,
            sod.nx_sku,
            sod.nx_cost_center,
            sod.nx_hdate_entered,
            sod.nx_hship_by_date,
            sod.nx_dship_by_date,
            sod.nx_hdue_date,
            sod.nx_ddue_date,
            sod.nx_qty_ordered,
            sod.nx_qty_balance_due,
            sod.nx_qty_committed,
            sod.nx_qty_allocated,
            sod.nx_item_status_desc,
            sod.nx_shipto_code,
            sod.nx_carrier,
            sod.nx_mos,
            sod.nx_part_status,
            sod.nx_last_updated,
            ps.part_desc,
            inv_st.on_hand               AS starlite_on_hand,
            inv_st.committed             AS starlite_committed,
            inv_st.allocated             AS starlite_allocated,
            inv_st.available             AS starlite_available,
            inv_hub.on_hand              AS hub_on_hand,
            wo.work_order_id             AS wo_id,
            wo.work_order_number         AS wo_number,
            wo.status                    AS wo_status,
            DATEDIFF(day, CAST(GETDATE() AS DATE), sod.nx_dship_by_date)
                                         AS days_to_ship,
            CASE WHEN sod.nx_hship_by_date <> sod.nx_dship_by_date
                   OR sod.nx_hdue_date    <> sod.nx_ddue_date
                 THEN 1 ELSE 0 END       AS dates_diverged,
            CASE WHEN sod.nx_dship_by_date <  CAST(GETDATE() AS DATE)
                  AND sod.nx_ddue_date    >= CAST(GETDATE() AS DATE)
                 THEN 1 ELSE 0 END       AS ship_by_stale,
            {rush_case}                  AS is_rush
        FROM dbo.sales_orders_due sod
        LEFT JOIN dbo.part_specs ps
               ON ps.sku = sod.nx_sku
        LEFT JOIN dbo.inventory_current inv_st
               ON inv_st.sku = sod.nx_sku
              AND inv_st.cost_center = 'Starlite'
        LEFT JOIN dbo.inventory_current inv_hub
               ON inv_hub.sku = sod.nx_sku
              AND inv_hub.cost_center = 'Hub'
        LEFT JOIN dbo.work_orders wo
               ON wo.sales_order_number = sod.nx_order_numb
              AND wo.sales_order_rel    = sod.nx_rel_numb
              AND CAST(wo.sales_order_line AS VARCHAR) = sod.nx_item_no
              AND wo.status NOT IN ('Complete', 'Cancelled')
        WHERE sod.nx_qty_balance_due > 0
          {date_pred}
    """
    params = list(date_params)

    if status_filter and status_filter != 'All':
      query += " AND sod.nx_item_status_desc = %s"
      params.append(status_filter)

    if search_text:
      query += """ AND (
            sod.nx_sku        LIKE %s
         OR sod.nx_order_numb LIKE %s
         OR sod.nx_shipto_code LIKE %s
        )"""
      wild = f"%{search_text}%"
      params.extend([wild, wild, wild])

    if has_allocated_only:
      query += (" AND ISNULL(sod.nx_qty_allocated, 0) "
                ">= ISNULL(sod.nx_qty_balance_due, 0)")

    if show_diverged_only:
      query += (" AND (sod.nx_hship_by_date <> sod.nx_dship_by_date "
                "OR sod.nx_hdue_date <> sod.nx_ddue_date)")

    if show_ship_by_stale_only:
      query += (" AND sod.nx_dship_by_date <  CAST(GETDATE() AS DATE) "
                "AND sod.nx_ddue_date    >= CAST(GETDATE() AS DATE)")

    if show_rush_only:
      query += " AND " + _rush_bool_sql('sod.')

    if created_on_date:
      query += " AND CAST(sod.nx_hdate_entered AS DATE) = %s"
      params.append(created_on_date)

    query += """
        ORDER BY sod.nx_dship_by_date ASC,
                 sod.nx_order_numb,
                 sod.nx_rel_numb,
                 sod.nx_item_no
    """

    cursor.execute(query, params)
    rows = cursor.fetchall()

    lines = []
    for row in rows:
      r = serialize_row(row)
      r['order_release']    = f"{r.get('nx_order_numb', '')}-{r.get('nx_rel_numb', '')}"
      r['sku']              = r.get('nx_sku', '')
      r['description']      = r.get('part_desc') or ''
      r['cost_center']      = r.get('nx_cost_center')
      r['shipto_code']      = r.get('nx_shipto_code') or ''
      r['nx_carrier']       = (r.get('nx_carrier') or '').strip()
      r['nx_mos']           = (r.get('nx_mos') or '').strip()
      r['is_rush']          = bool(r.get('is_rush'))
      r['item_status_desc'] = r.get('nx_item_status_desc') or ''
      r['item_no']          = r.get('nx_item_no')
      r['order_numb']       = r.get('nx_order_numb')
      r['rel_numb']         = r.get('nx_rel_numb')
      r['qty_ordered']      = r.get('nx_qty_ordered')
      r['qty_balance_due']  = r.get('nx_qty_balance_due')
      r['qty_committed']    = r.get('nx_qty_committed')
      r['qty_allocated']    = r.get('nx_qty_allocated')

      r['header_create_date'] = _fmt_date(r.get('nx_hdate_entered'))
      r['ship_by_date']     = _fmt_date(r.get('nx_dship_by_date'))
      r['header_ship_by']   = _fmt_date(r.get('nx_hship_by_date'))
      r['due_date']         = _fmt_date(r.get('nx_ddue_date'))
      r['header_due']       = _fmt_date(r.get('nx_hdue_date'))
      r['ship_date_diverged'] = bool(r.get('dates_diverged'))
      r['ship_by_stale']      = bool(r.get('ship_by_stale'))

      lines.append(r)

    cursor.execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN nx_dship_by_date < CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS late_ship,
            SUM(CASE WHEN nx_ddue_date    < CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS late_due,
            SUM(CASE WHEN nx_dship_by_date = CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS due_today,
            SUM(CASE WHEN nx_dship_by_date BETWEEN CAST(GETDATE() AS DATE)
                                              AND DATEADD(day, 7,
                                                   CAST(GETDATE() AS DATE))
                     THEN 1 ELSE 0 END) AS due_7,
            SUM(CASE WHEN nx_item_status_desc = 'Hold'
                     THEN 1 ELSE 0 END) AS on_hold,
            SUM(CASE WHEN nx_hship_by_date <> nx_dship_by_date
                       OR nx_hdue_date    <> nx_ddue_date
                     THEN 1 ELSE 0 END) AS diverged_dates,
            SUM(CASE WHEN nx_dship_by_date <  CAST(GETDATE() AS DATE)
                      AND nx_ddue_date    >= CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS ship_by_stale,
            SUM({_rush_case_sql('')}) AS rush,
            SUM(CASE WHEN DATEDIFF(day, nx_dship_by_date,
                                    CAST(GETDATE() AS DATE))
                          BETWEEN 1 AND 7
                     THEN 1 ELSE 0 END) AS aging_1_7,
            SUM(CASE WHEN DATEDIFF(day, nx_dship_by_date,
                                    CAST(GETDATE() AS DATE))
                          BETWEEN 8 AND 30
                     THEN 1 ELSE 0 END) AS aging_8_30,
            SUM(CASE WHEN DATEDIFF(day, nx_dship_by_date,
                                    CAST(GETDATE() AS DATE)) >= 31
                     THEN 1 ELSE 0 END) AS aging_31_plus
        FROM dbo.sales_orders_due
        WHERE nx_qty_balance_due > 0
    """)
    c = cursor.fetchone()

    counts = {
      'total':          c['total'] or 0,
      'late_ship':      c['late_ship'] or 0,
      'late_due':       c['late_due'] or 0,
      'due_today':      c['due_today'] or 0,
      'due_7':          c['due_7'] or 0,
      'on_hold':        c['on_hold'] or 0,
      'diverged_dates': c['diverged_dates'] or 0,
      'ship_by_stale':  c['ship_by_stale'] or 0,
      'rush':           c['rush'] or 0,
      'aging': {
        '1_7':     c['aging_1_7'] or 0,
        '8_30':    c['aging_8_30'] or 0,
        '31_plus': c['aging_31_plus'] or 0,
      },
    }

    conn.close()

    return {
      'status':         'success',
      'lines':          lines,
      'counts':         counts,
      'last_refreshed': datetime.now().isoformat(),
    }

  except Exception as e:
    return {
      'status':  'error',
      'message': str(e),
      'lines':   [],
      'counts':  {},
    }


# =============================================================================
# LIGHTWEIGHT STATS (summary cards only)
# =============================================================================

@anvil.server.callable
def get_sales_orders_due_stats():
  """Return summary counts only — unchanged from v5.15.0."""
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN nx_dship_by_date < CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS late_ship,
            SUM(CASE WHEN nx_ddue_date    < CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS late_due,
            SUM(CASE WHEN nx_dship_by_date = CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS due_today,
            SUM(CASE WHEN nx_dship_by_date BETWEEN CAST(GETDATE() AS DATE)
                                              AND DATEADD(day, 7,
                                                   CAST(GETDATE() AS DATE))
                     THEN 1 ELSE 0 END) AS due_7,
            SUM(CASE WHEN nx_item_status_desc = 'Hold'
                     THEN 1 ELSE 0 END) AS on_hold,
            SUM(CASE WHEN nx_hship_by_date <> nx_dship_by_date
                       OR nx_hdue_date    <> nx_ddue_date
                     THEN 1 ELSE 0 END) AS diverged_dates,
            SUM(CASE WHEN nx_dship_by_date <  CAST(GETDATE() AS DATE)
                      AND nx_ddue_date    >= CAST(GETDATE() AS DATE)
                     THEN 1 ELSE 0 END) AS ship_by_stale,
            SUM({_rush_case_sql('')}) AS rush
        FROM dbo.sales_orders_due
        WHERE nx_qty_balance_due > 0
    """)
    row = cursor.fetchone()
    conn.close()
    return {
      'status':         'success',
      'total_open':     row['total'] or 0,
      'late_ship':      row['late_ship'] or 0,
      'late_due':       row['late_due'] or 0,
      'due_today':      row['due_today'] or 0,
      'due_7':          row['due_7'] or 0,
      'on_hold':        row['on_hold'] or 0,
      'diverged_dates': row['diverged_dates'] or 0,
      'ship_by_stale':  row['ship_by_stale'] or 0,
      'rush':           row['rush'] or 0,
      'last_refreshed': datetime.now().isoformat(),
    }
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# SINGLE LINE DETAIL (for the SO Detail panel) — unchanged from v5.15.0
# =============================================================================

@anvil.server.callable
def get_sales_order_line_detail(order_numb, rel_numb, item_no):
  """Single-line lookup with full notes, joined inventory snapshot for
  BOTH cost centers, and any active work order."""
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            sod.*,
            ps.part_desc,
            inv_st.on_hand        AS starlite_on_hand,
            inv_st.committed      AS starlite_committed,
            inv_st.allocated      AS starlite_allocated,
            inv_st.available      AS starlite_available,
            inv_st.on_po          AS starlite_on_po,
            inv_st.on_blanket_po  AS starlite_on_blanket_po,
            inv_hub.on_hand       AS hub_on_hand,
            inv_hub.committed     AS hub_committed,
            inv_hub.available     AS hub_available,
            wo.work_order_id,
            wo.work_order_number,
            wo.status             AS wo_status,
            wo.due_date           AS wo_due_date,
            CASE WHEN bo.nx_order_numb IS NOT NULL THEN 1 ELSE 0 END AS has_backorder
        FROM dbo.sales_orders_due sod
        LEFT JOIN dbo.part_specs ps
               ON ps.sku = sod.nx_sku
        LEFT JOIN dbo.inventory_current inv_st
               ON inv_st.sku = sod.nx_sku AND inv_st.cost_center = 'Starlite'
        LEFT JOIN dbo.inventory_current inv_hub
               ON inv_hub.sku = sod.nx_sku AND inv_hub.cost_center = 'Hub'
        LEFT JOIN dbo.work_orders wo
               ON wo.sales_order_number = sod.nx_order_numb
              AND wo.sales_order_rel    = sod.nx_rel_numb
              AND CAST(wo.sales_order_line AS VARCHAR) = sod.nx_item_no
              AND wo.status NOT IN ('Complete', 'Cancelled')
        LEFT JOIN dbo.sales_backorders bo
               ON bo.nx_order_numb = sod.nx_order_numb
              AND bo.nx_rel_numb   = sod.nx_rel_numb
              AND bo.nx_item_no    = sod.nx_item_no
              AND bo.nx_baldue > 0
        WHERE sod.nx_order_numb = %s
          AND sod.nx_rel_numb   = %s
          AND sod.nx_item_no    = %s
    """, [order_numb, rel_numb, item_no])

    row = cursor.fetchone()
    conn.close()

    if not row:
      return {'status': 'error', 'message': 'Line not found'}

    line = serialize_row(row)
    line['order_release']  = f"{line.get('nx_order_numb', '')}-{line.get('nx_rel_numb', '')}"
    line['ship_by_date']   = _fmt_date(line.get('nx_dship_by_date'))
    line['header_ship_by'] = _fmt_date(line.get('nx_hship_by_date'))
    line['due_date']       = _fmt_date(line.get('nx_ddue_date'))
    line['header_due']     = _fmt_date(line.get('nx_hdue_date'))
    line['ship_date_diverged'] = (
      line.get('nx_hship_by_date') != line.get('nx_dship_by_date')
      or line.get('nx_hdue_date') != line.get('nx_ddue_date')
    )
    sb = line.get('nx_dship_by_date')
    dd = line.get('nx_ddue_date')
    today_iso = date.today().isoformat()
    line['ship_by_stale'] = bool(
      sb is not None and dd is not None
      and str(sb)[:10] < today_iso <= str(dd)[:10]
    )
    line['has_backorder']  = bool(line.get('has_backorder'))
    return {'status': 'success', 'line': line}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# EXCEL EXPORT — v5.17.0 revamp
# =============================================================================

# Column layout per Production Control's 2026-05-19 spec.
# Each entry: (header, getter, width, horizontal_align)
#   width = None means "leave at Excel default column width"
#   horizontal_align = None means "default (text-left, number-right)"
_EXPORT_COLUMNS = [
  ('Days',          lambda ln: ln.get('days_to_ship'),       10,    'center'),
  ('Status',        lambda ln: ln.get('item_status_desc'),   25,    'center'),
  ('Order #',       lambda ln: ln.get('order_release'),      15,    'left'),
  ('Item',          lambda ln: ln.get('item_no'),            10,    'left'),
  ('SKU',           lambda ln: ln.get('sku'),                25,    'left'),
  ('Ship-To',       lambda ln: ln.get('shipto_code'),        30,    'left'),
  ('Qty Ord',       lambda ln: ln.get('qty_ordered'),        20,    'right'),
  ('Qty Bal Due',   lambda ln: ln.get('qty_balance_due'),    20,    'right'),
  ('Qty Committed', lambda ln: ln.get('qty_committed'),      20,    'right'),
  ('Qty Allocated', lambda ln: ln.get('qty_allocated'),      20,    'right'),
  ('Created',       lambda ln: ln.get('header_create_date'),  15,    'right'),
  ('Ship By',       lambda ln: ln.get('ship_by_date'),       20,    'right'),
  ('Due',           lambda ln: ln.get('due_date'),           20,    'right'),
  ('Starlite OH',   lambda ln: ln.get('starlite_on_hand'),   15,    'right'),
  ('Hub OH',        lambda ln: ln.get('hub_on_hand'),        15,    'right'),
  ('WO #',          lambda ln: ln.get('wo_number') or '',    20,    'right'),
]


@anvil.server.callable
def export_sales_orders_due_excel(filters=None):
  """Re-runs the same filtered query as get_sales_orders_due, then writes
  an .xlsx via openpyxl with a frozen header row, autofilter, conditional
  color fill on the Days column, and per-column widths and alignments
  matching the v5.17.0 Production Control spec.

  Returns a BlobMedia the client can hand to anvil.media.download().
  """
  try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    filters = filters or {}
    result = get_sales_orders_due(
      view_mode=filters.get('view_mode', 'Late + Next 7 Days'),
      status_filter=filters.get('status_filter'),
      search_text=filters.get('search_text'),
      has_allocated_only=filters.get('has_allocated_only', False),
      show_diverged_only=filters.get('show_diverged_only', False),
      show_ship_by_stale_only=filters.get('show_ship_by_stale_only', False),
      show_rush_only=filters.get('show_rush_only', False),
      created_on_date=filters.get('created_on_date'),
      custom_start=filters.get('custom_start'),
      custom_end=filters.get('custom_end'),
    )
    if result['status'] != 'success':
      return {'status': 'error', 'message': result.get('message')}

    lines = result['lines']

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Open Lines'

    # ---- Header row ----------------------------------------------------
    headers = [col[0] for col in _EXPORT_COLUMNS]
    ws.append(headers)

    header_font = Font(bold=True, color='FFFFFF', size=11)
    header_fill = PatternFill('solid', fgColor='1F4E79')
    header_align = Alignment(horizontal='center', vertical='center')
    for col_idx in range(1, len(headers) + 1):
      cell = ws.cell(row=1, column=col_idx)
      cell.font = header_font
      cell.fill = header_fill
      cell.alignment = header_align

    # ---- Days-column conditional fills (preserved from v5.14.x) --------
    red_fill    = PatternFill('solid', fgColor='F8D7DA')
    amber_fill  = PatternFill('solid', fgColor='FFE5B4')
    yellow_fill = PatternFill('solid', fgColor='FFF3CD')
    green_fill  = PatternFill('solid', fgColor='D4EDDA')

    # Pre-build per-column Alignment objects so we don't reallocate per row
    align_per_col = []
    for _, _, _, h_align in _EXPORT_COLUMNS:
      if h_align is None:
        # Default horizontal alignment — let Excel decide based on data type
        align_per_col.append(Alignment(vertical='center'))
      else:
        align_per_col.append(Alignment(horizontal=h_align, vertical='center'))

    # ---- Data rows -----------------------------------------------------
    for line in lines:
      values = [getter(line) for (_, getter, _, _) in _EXPORT_COLUMNS]
      ws.append([_excel_safe(v) for v in values])

      r = ws.max_row
      # Apply per-column alignment
      for col_idx in range(1, len(_EXPORT_COLUMNS) + 1):
        ws.cell(row=r, column=col_idx).alignment = align_per_col[col_idx - 1]

      # Conditional fill on the Days column (column 1)
      days = line.get('days_to_ship')
      days_cell = ws.cell(row=r, column=1)
      if days is None:
        pass
      elif days < 0:
        days_cell.fill = red_fill
        days_cell.font = Font(bold=True, color='9C0006')
      elif days == 0:
        days_cell.fill = amber_fill
        days_cell.font = Font(bold=True)
      elif days <= 3:
        days_cell.fill = yellow_fill
      elif days <= 7:
        days_cell.fill = green_fill

    # ---- Column widths (only the ones specified in the spec) -----------
    for idx, (_, _, width, _) in enumerate(_EXPORT_COLUMNS, start=1):
      if width is not None:
        ws.column_dimensions[get_column_letter(idx)].width = width

    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions

    # ---- Summary sheet (unchanged from v5.15.0) ------------------------
    ws2 = wb.create_sheet('Summary')
    c = result['counts']
    summary_rows = [
      ['Sales Orders Due — Snapshot', ''],
      ['Generated', datetime.now().strftime('%Y-%m-%d %H:%M')],
      ['View Mode', filters.get('view_mode', 'Late + Next 7 Days')],
      ['', ''],
      ['Total Open Lines', c.get('total', 0)],
      ['Late by Ship Date', c.get('late_ship', 0)],
      ['Late by Due Date', c.get('late_due', 0)],
      ['Due Today', c.get('due_today', 0)],
      ['Due in Next 7 Days', c.get('due_7', 0)],
      ['On Hold', c.get('on_hold', 0)],
      ['Diverged Header/Detail Dates', c.get('diverged_dates', 0)],
      ['Stale Ship-By (ship past, due future)', c.get('ship_by_stale', 0)],
      ['', ''],
      ['Aging — 1-7 days late', c.get('aging', {}).get('1_7', 0)],
      ['Aging — 8-30 days late', c.get('aging', {}).get('8_30', 0)],
      ['Aging — 31+ days late',  c.get('aging', {}).get('31_plus', 0)],
    ]
    for r in summary_rows:
      ws2.append(r)
    ws2.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws2.column_dimensions['A'].width = 36
    ws2.column_dimensions['B'].width = 24

    # ---- Serialize and return as BlobMedia ----------------------
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    view_tag = (filters.get('view_mode') or 'AllOpen').replace(' ', '').replace('+', 'plus')
    filename = f"SalesOrdersDue_{view_tag}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    media = anvil.BlobMedia(
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      buf.read(),
      name=filename
    )
    return {'status': 'success', 'media': media, 'row_count': len(lines)}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}