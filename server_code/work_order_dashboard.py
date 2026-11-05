"""
Anvil Server Module - Work Order Dashboard
===========================================
Server-side code for the Titan Flow Control Work Order system.

This module provides database connectivity and business logic for:
    - Fetching work orders with filtering and search
    - Updating work order status (Created → In Progress → Complete)
    - Marking work orders as printed
    - Summary counts for dashboard cards

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: February 2026

2026-09-17 (wo-status-ownership DECISION step 4): when PC closes a work
order administratively - Accept Orphans (bulk Verify -> Complete) or a
single Cancel / Complete through update_work_order_status - every routing
step still open on it (Pending / Ready / In Progress) is SKIPPED with a
'Skip' event, so the routing never contradicts the work order again and
no department queue keeps a phantom 'Ready' row for finished work. Skip
is the routing engine's own PC idiom (record_step_event 'Skip',
migration 007) - no new vocabulary.
2026-09-24 PERFORMANCE (perf_probe measured the office grid at 3.6 s
server-side: 0.7 s connection, 0.9 s SQL across 7 statements, 2.0 s pulling
a 4 MB result set - 1.6 KB per row, almost all of it the three varchar(max)
notes columns nobody reads until they click):
  - NOTES ON DEMAND: get_work_orders ships has_notes + a 60-char preview;
    the full SKU / SO notes come from get_wo_notes(work_order_id) when the
    Notes link or the detail popup opens (the SE app has done it this way
    since June).
  - Column probes (plan / sku_desc) cached per server process instead of
    two round trips per call; the three scalar count queries collapsed into
    one statement. 7 statements -> 4.
2026-09-23: that work moved INTO THE DATABASE - dbo.sp_pc_accept_orphans
(the whole bulk close-out) and dbo.sp_pc_close_wo_routing (one WO),
migration 084. The 09-17 version looped in Python, two statements per open
step; on 09-23 Accept Orphans hit 228 open steps (~456 round trips) and
died on Anvil's 30 s call limit. Each caller is now ONE EXEC.
2026-11-05 (PC meeting - "what do I focus on next?"):
  - get_work_orders(so_status=...): 'Rush' | 'Allocated' | 'Entered' |
    'Hold' filters on the e21 line (rush = PC's carrier+method rule, the
    others = sales_orders_due.nx_item_status_desc). Three count chips on
    the Work Orders form drive it; counts['allocated' / 'entered' / 'hold'].
  - HELD LINES LEAVE THE WORK ORDERS GRID: unless so_status='Hold' is
    asked for, a work order whose e21 line is on Hold is not returned and
    not counted in the status cards. The Exceptions form shows them.
2026-10-06 (Production Control v2.0, Routing Desk slice 1):
  - RELEASE IS GUARDED: update_work_order_status('Released') now delegates to
    routing_desk.release_work_order, so no path can put a work order on the
    floor without a saved routing (the old editor flow keeps working: the
    release only readies steps that are still Pending and texts for those).
  - UNRELEASE DEMOTES: unrelease_work_order puts Ready steps back to Pending
    (Edit event 'un-released') and parks any text still Queued, so an
    un-released work order leaves every department queue (fix F1 / M5).
  - Plan chip: denominator = every WO on today's plan except Cancelled,
    numerator = everything past Created (fix F6 / M4). No more counting down.
  - get_work_orders ships wo_type (Standard / Non-Standard).
"""

# =============================================================================
# Version:       2.0.1
# Last updated:  2026-10-06
# Changelog (this module only; versioning starts here per Dev-Kit rule):
#   2.0.0 (2026-10-06) Release delegated to routing_desk (guarded); UnRelease
#                      demotes Ready steps; plan-chip counts fixed; wo_type
#                      added to the grid payload.
# =============================================================================

__version__       = "2.0.1"
__version_date__  = "2026-10-06"
__version_notes__ = ("Release guarded via routing_desk; UnRelease demotes Ready steps; "
                     "plan chip math; wo_type in grid rows")

import anvil.server
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config
from . import perf_probe   # 2026-09-24 timing probe - remove with perf_probe.py
from . import sales_orders_due_dashboard as _sod  # rush-method whitelist (single source)
from . import routing_desk as _desk  # v2.0: guarded release + UnRelease demotion (single write path)


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================

def make_serializable(value):
  """
    Convert non-serializable types to serializable ones.
    Anvil can't serialize datetime, Decimal, bytes directly.
  """
  if value is None:
    return None
  elif isinstance(value, (datetime, date)):
    return value.isoformat()
  elif isinstance(value, Decimal):
    return float(value)
  elif isinstance(value, bytes):
    return value.decode('utf-8', errors='replace')
  else:
    return value


def serialize_row(row):
  """Convert a row dict to all serializable values."""
  return {key: make_serializable(val) for key, val in row.items()}


# =============================================================================
# DATABASE CONNECTION
# =============================================================================

def get_connection():
  """
    Create and return a database connection.
    Uses centralized db_config for credentials.
  """
  return db_config.get_nexus_connection()


# =============================================================================
# DAILY PLAN STAMP (migration 056, 2026-09-08)
# =============================================================================
# The published Daily Plan is carried on work_orders.plan_date / plan_tier /
# plan_position (stamped by the Daily Plan app on publish + revision, and by
# scan_and_create_work_orders for WOs born after publish). Today's plan sorts
# FIRST, in plan order, so releasing the plan is a top-to-bottom pass.

_WO_TERMINAL = ('Complete', 'Cancelled', 'Closed - Verify',
                'Shipped - Verify', 'Invoiced - Verify')


# Column probes are answered ONCE per server process (2026-09-24): the
# answer cannot change under a running process except by a migration, and
# each probe was a 60-70 ms round trip on every grid load.
_COLUMN_CACHE = {}


def _has_column(cursor, table, column):
  key = (table, column)
  if key not in _COLUMN_CACHE:
    cursor.execute("SELECT COL_LENGTH(%s, %s) AS c", (table, column))
    row = cursor.fetchone()
    _COLUMN_CACHE[key] = bool(row and row.get('c') is not None)
  return _COLUMN_CACHE[key]


def _has_plan_columns(cursor):
  """True once 056 is applied. Lets this module be pasted before the
  migration runs without breaking the grid."""
  return _has_column(cursor, 'dbo.work_orders', 'plan_date')


def _has_desc_column(cursor):  # 058; cached per process (see _has_column)
  return _has_column(cursor, 'dbo.work_orders', 'sku_desc')


def _plan_label(tier, position, plan_date_str, on_today):
  """'M-3' for today's plan; 'M-3 (09/08)' when the stamp is an older day
  (the line was planned, never moved, and is not on today's plan)."""
  if not tier or position is None:
    return ''
  label = f"{str(tier)[0].upper()}-{int(position)}"
  if not on_today and plan_date_str:
    try:
      from datetime import datetime as dt
      label += f" ({dt.fromisoformat(plan_date_str).strftime('%m/%d')})"
    except (ValueError, TypeError):
      pass
  return label


# =============================================================================
# GET WORK ORDERS
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def get_work_orders(status_filter=None, search_text=None, active_only=False, backorder_only=False, statuses=None, show_rush_only=False, limit=4000, so_status=None):
  """
    Fetch work orders with optional filtering.

    Args:
        status_filter:  any work_orders.status value
                        ('Created', 'Released', 'Assigned', 'In Progress',
                         'Paused', 'Complete', 'Cancelled', ...) or None for all
        search_text:    Search across WO#, SO#, and Part#
        active_only:    When True, hide Complete/Cancelled work orders
        backorder_only: When True, show only work orders on backorder releases
                        (sales_order_rel <> '001')
        statuses:       optional list of statuses to include (IN filter);
                        used by the condensed Active view
        so_status:      'Rush' | 'Allocated' | 'Entered' | 'Hold' (2026-11-05):
                        filter on the e21 sales-order line. Anything but
                        'Hold' ALSO hides held lines (they live on the
                        Exceptions form); 'Hold' returns only held lines.

    Returns:
        dict with:
            - 'status': 'success' or 'error'
            - 'work_orders': list of serialized row dicts
            - 'counts': dict of status counts for summary cards
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # -----------------------------------------------------------------
    # Main query: work orders joined to sales_orders_due for context
    # -----------------------------------------------------------------
    rush_case = _sod._rush_case_sql('so.')
    # Volume guard (2026-08-31): the all-WO expansion (migration 037) grew
    # work_orders past what one Anvil server-call return can carry. Cap the
    # rows shipped to the client; TOP(limit+1) lets us detect truncation.
    top_clause = ''
    if limit:
      top_clause = f'TOP ({int(limit) + 1}) '
    has_plan = _has_plan_columns(cursor)
    # 058 (2026-09-10): the SO line's description. Non-stock lines (part
    # status N - 'GASKET ONLY', 'SCREEN ONLY' ...) carry their real spec
    # here. Live value while the SO line is on the book, stored copy after.
    if _has_desc_column(cursor):
      desc_col = "COALESCE(so.nx_sku_desc, wo.sku_desc) AS sku_desc,"
    else:
      desc_col = "so.nx_sku_desc AS sku_desc,"
    if has_plan:
      plan_cols = ("\n            wo.plan_date, wo.plan_tier, wo.plan_position,"
                   "\n            CASE WHEN wo.plan_date = CAST(GETDATE() AS DATE)"
                   " THEN 1 ELSE 0 END AS on_todays_plan,")
    else:
      plan_cols = ("\n            CAST(NULL AS DATE) AS plan_date,"
                   " CAST(NULL AS VARCHAR(8)) AS plan_tier,"
                   "\n            CAST(NULL AS INT) AS plan_position, 0 AS on_todays_plan,")
    query = f"""
        SELECT {top_clause}
            wo.work_order_id,{plan_cols}
            wo.work_order_number,
            wo.wo_type,
            wo.sales_order_number,
            wo.sales_order_rel,
            wo.sales_order_line,
            wo.part_number,
            wo.part_attribute,
            wo.attribute_value,
            wo.attribute_desc,
            wo.quantity,
            wo.wo_override_qty,
            wo.due_date,
            wo.status,
            wo.created_at,
            wo.printed_at,
            wo.completed_at,
            wo.created_by,
            wo.completed_by,
            wo.pending_change_count,
            wo.last_change_detected_at,
            wo.wo_due_date,
            CASE WHEN (wo.notes IS NOT NULL AND LTRIM(RTRIM(CAST(wo.notes AS varchar(200)))) <> '')
                   OR (so.nx_notes IS NOT NULL AND LTRIM(RTRIM(CAST(so.nx_notes AS varchar(200)))) <> '')
                   OR (wo.so_notes IS NOT NULL AND LTRIM(RTRIM(CAST(wo.so_notes AS varchar(200)))) <> '')
                   OR (so.nx_so_notes IS NOT NULL AND LTRIM(RTRIM(CAST(so.nx_so_notes AS varchar(200)))) <> '')
                 THEN 1 ELSE 0 END                       AS has_notes,
            LEFT(CAST(COALESCE(so.nx_notes, wo.notes, so.nx_so_notes, wo.so_notes) AS varchar(200)), 60) AS notes_preview,
            COALESCE(so.nx_attrib_val, wo.attribute_value) AS so_attrib_val,
            {desc_col}
            CASE WHEN so.nx_part_status = 'N' THEN 1 ELSE 0 END AS is_nonstock,
            so.nx_dship_by_date AS so_ship_by_date,
            ad.nx_attribute_value AS standard_attrib_val,
            so.nx_carrier,
            so.nx_mos,
            {rush_case} AS is_rush
        FROM dbo.work_orders wo
        LEFT JOIN dbo.sales_orders_due so
            ON wo.sales_order_number = so.nx_order_numb
           AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
           AND wo.sales_order_rel = so.nx_rel_numb 
        LEFT JOIN dbo.attrib_val_default ad
            ON wo.part_number = ad.nx_part_code
        WHERE 1=1
    """
    params = []

    if status_filter:
      if status_filter == 'Pending Changes':
        query += " AND wo.pending_change_count > 0"
        query += " AND wo.status NOT IN ('Complete')"
      else:
        query += " AND wo.status = %s"
        params.append(status_filter)

    # Multi-status filter (e.g. the condensed Active view: Created/Released/In Progress)
    if statuses:
      _ph = ", ".join(["%s"] * len(statuses))
      query += f" AND wo.status IN ({_ph})"
      params.extend(statuses)

    if active_only:
      query += " AND wo.status NOT IN ('Complete', 'Cancelled')"

    if backorder_only:
      # Backorders = any release other than the original '001'
      query += " AND wo.sales_order_rel <> '001'"

    if show_rush_only:
      query += " AND " + _sod._rush_bool_sql('so.')

    # 2026-11-05: e21 line-status filters + the hold rule. A held line is
    # an exception, not work: it never shows on the Work Orders grid.
    if so_status == 'Hold':
      query += " AND so.nx_item_status_desc = 'Hold'"
    else:
      query += " AND ISNULL(so.nx_item_status_desc, '') <> 'Hold'"
      if so_status == 'Rush':
        query += " AND " + _sod._rush_bool_sql('so.')
      elif so_status in ('Allocated', 'Entered'):
        query += " AND so.nx_item_status_desc = %s"
        params.append(so_status)

    if search_text:
      query += """ AND (
        wo.work_order_number LIKE %s
        OR wo.sales_order_number LIKE %s
        OR CONCAT(wo.sales_order_number, '-', wo.sales_order_rel) LIKE %s
        OR wo.part_number LIKE %s
    )"""
      search_param = f"%{search_text}%"
      params.extend([search_param, search_param, search_param, search_param])

    # Sort: live/floor statuses first, then office backlog, then terminal.
    # Within each group, sort by due date ascending (earliest first).
    # SE Department rollout (2026-06-17): Released/Assigned/Paused added.
    # 056: today's published plan first, in plan order; everything else keeps
    # the status-group / due-date order below it.
    if has_plan:
      query += """
        ORDER BY
            CASE WHEN wo.plan_date = CAST(GETDATE() AS DATE) THEN 0 ELSE 1 END,
            ISNULL(wo.plan_position, 2147483647),"""
    else:
      query += """
        ORDER BY"""
    query += """
            CASE wo.status
                WHEN 'In Progress' THEN 1
                WHEN 'Paused'      THEN 2
                WHEN 'Released'    THEN 3
                WHEN 'Assigned'    THEN 4
                WHEN 'Created'     THEN 5
                WHEN 'Complete'    THEN 6
                ELSE 7
            END,
            wo.due_date ASC
    """

    cursor.execute(query, params)
    rows = cursor.fetchall()

    # Volume guard: detect and trim past-cap results
    truncated = bool(limit) and len(rows) > int(limit)
    if truncated:
      rows = rows[:int(limit)]

    # Serialize and split attribute values for grid display
    work_orders = []
    for row in rows:
      serialized = serialize_row(row)

      # Split pipe-delimited attribute_value into material and SE
      attr_val = (serialized.get('attribute_value') or '').strip()
      parts = [p.strip() for p in attr_val.split('|')]
      serialized['material_value'] = parts[0] if len(parts) > 0 else ''

      # Combined SO# + Release for display (e.g. "589808-002")
      rel = serialized.get('sales_order_rel') or '001'
      serialized['so_number_display'] = f"{serialized.get('sales_order_number', '')}-{rel}"

      # Format dates for clean display
      if serialized.get('created_at'):
        try:
          from datetime import datetime as dt
          created = dt.fromisoformat(serialized['created_at'])
          serialized['created_at'] = created.strftime('%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          pass

      if serialized.get('due_date'):
        try:
          from datetime import datetime as dt
          due = dt.fromisoformat(serialized['due_date'])
          serialized['due_date'] = due.strftime('%m/%d/%Y')
        except (ValueError, TypeError):
          pass

      if serialized.get('wo_due_date'):
        try:
          from datetime import datetime as dt
          wo_due = dt.fromisoformat(serialized['wo_due_date'])
          serialized['wo_due_date'] = wo_due.strftime('%m/%d/%Y')
        except (ValueError, TypeError):
          pass

      serialized['se_value'] = parts[1] if len(parts) > 1 else ''
      # Concatenated 'Material / SE' for the condensed grid
      _mat = serialized['material_value']
      _se = serialized['se_value']
      if _mat and _se:
        serialized['material_se'] = f"{_mat} / {_se}"
      else:
        serialized['material_se'] = _mat or _se or ''

      # SO line description (058) - the spec for non-stock lines
      serialized['sku_desc'] = (serialized.get('sku_desc') or '').strip()
      serialized['is_nonstock'] = int(serialized.get('is_nonstock') or 0)

      # Daily Plan stamp (056): 'M-3' / 'N-12' / 'S-2' (+ date when not today)
      serialized['on_todays_plan'] = int(serialized.get('on_todays_plan') or 0)
      serialized['plan_label'] = _plan_label(serialized.get('plan_tier'),
                                             serialized.get('plan_position'),
                                             serialized.get('plan_date'),
                                             serialized['on_todays_plan'])

      # Notes on demand (2026-09-24): the grid carries a flag + preview only;
      # get_wo_notes() fetches the text when someone opens it.
      serialized['has_notes'] = int(serialized.get('has_notes') or 0)
      serialized['notes_preview'] = (serialized.get('notes_preview') or '').strip()

      # Carrier / MOS / rush flag (Carrier+MOS+Rush feature)
      serialized['nx_carrier'] = (serialized.get('nx_carrier') or '').strip()
      serialized['nx_mos']     = (serialized.get('nx_mos') or '').strip()
      serialized['is_rush']    = bool(serialized.get('is_rush'))

      work_orders.append(serialized)

    # -----------------------------------------------------------------
    # Summary counts (always unfiltered for the cards)
    # -----------------------------------------------------------------
    # 2026-11-05: held e21 lines are excluded from the cards, as from the grid.
    cursor.execute("""
        SELECT wo.status, COUNT(*) AS cnt
        FROM dbo.work_orders wo
        LEFT JOIN dbo.sales_orders_due so
          ON wo.sales_order_number = so.nx_order_numb
         AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
         AND wo.sales_order_rel = so.nx_rel_numb
        WHERE ISNULL(so.nx_item_status_desc, '') <> 'Hold'
        GROUP BY wo.status
    """)
    counts = {row['status']: row['cnt'] for row in cursor.fetchall()}

    # One statement for the three scalar counts (2026-09-24; was three
    # round trips): pending changes, rush, today's plan.
    plan_sql = "0 AS plan_today, 0 AS plan_released"
    plan_params = ()
    if has_plan:
      # v2.0 (F6): the chip reads "released / planned". Planned = every WO on
      # today's plan except Cancelled; released = everything past Created.
      # Completed work stays in BOTH numbers, so the chip no longer counts
      # down during the day.
      plan_sql = """
            (SELECT COUNT(*) FROM dbo.work_orders
              WHERE plan_date = CAST(GETDATE() AS DATE)
                AND status <> 'Cancelled') AS plan_today,
            (SELECT COUNT(*) FROM dbo.work_orders
              WHERE plan_date = CAST(GETDATE() AS DATE)
                AND status NOT IN ('Created', 'Cancelled')) AS plan_released"""
      plan_params = ()
    cursor.execute(f"""
        SELECT
            (SELECT COUNT(*) FROM dbo.work_orders
              WHERE pending_change_count > 0 AND status NOT IN ('Complete')) AS pending_changes,
            (SELECT COUNT(*)
               FROM dbo.work_orders wo
               LEFT JOIN dbo.sales_orders_due so
                 ON wo.sales_order_number = so.nx_order_numb
                AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
                AND wo.sales_order_rel = so.nx_rel_numb
              WHERE wo.status IN ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')
                AND ISNULL(so.nx_item_status_desc, '') <> 'Hold'
                AND {_sod._rush_bool_sql('so.')}) AS rush,
            (SELECT COUNT(*) FROM dbo.work_orders wo
               JOIN dbo.sales_orders_due so
                 ON wo.sales_order_number = so.nx_order_numb
                AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
                AND wo.sales_order_rel = so.nx_rel_numb
              WHERE wo.status IN ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')
                AND so.nx_item_status_desc = 'Allocated') AS allocated,
            (SELECT COUNT(*) FROM dbo.work_orders wo
               JOIN dbo.sales_orders_due so
                 ON wo.sales_order_number = so.nx_order_numb
                AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
                AND wo.sales_order_rel = so.nx_rel_numb
              WHERE wo.status IN ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')
                AND so.nx_item_status_desc = 'Entered') AS entered,
            (SELECT COUNT(*) FROM dbo.work_orders wo
               JOIN dbo.sales_orders_due so
                 ON wo.sales_order_number = so.nx_order_numb
                AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
                AND wo.sales_order_rel = so.nx_rel_numb
              WHERE wo.status NOT IN ('Complete', 'Cancelled')
                AND so.nx_item_status_desc = 'Hold') AS hold,
            {plan_sql}
    """, plan_params)
    _sc = cursor.fetchone() or {}
    counts['PendingChanges'] = _sc.get('pending_changes') or 0
    counts['rush'] = _sc.get('rush') or 0
    counts['allocated'] = _sc.get('allocated') or 0
    counts['entered'] = _sc.get('entered') or 0
    counts['hold'] = _sc.get('hold') or 0
    counts['plan_today'] = _sc.get('plan_today') or 0
    counts['plan_released'] = _sc.get('plan_released') or 0

    conn.close()

    return {
      'status': 'success',
      'work_orders': work_orders,
      'counts': counts,
      'truncated': truncated
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e),
      'work_orders': [],
      'counts': {}
    }


# =============================================================================
# GET WORK ORDER NOTES  (on demand - 2026-09-24)
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def get_wo_notes(work_order_id):
  """
    The full notes for ONE work order, fetched when the Notes link or the
    detail popup opens. Same three values the grid used to carry on every
    row (wo.notes, SKU-level and SO-level notes with the live SO line
    winning over the stored copy), now one small call per click instead
    of ~1.6 KB x thousands of rows per grid load.

    Returns:
        {'status': 'success', 'notes', 'so_notes', 'so_level_notes'} or error
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT wo.notes,
               COALESCE(so.nx_notes, wo.notes)       AS so_notes,
               COALESCE(so.nx_so_notes, wo.so_notes) AS so_level_notes
        FROM dbo.work_orders wo
        LEFT JOIN dbo.sales_orders_due so
          ON wo.sales_order_number = so.nx_order_numb
         AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
         AND wo.sales_order_rel = so.nx_rel_numb
        WHERE wo.work_order_id = %s
    """, (work_order_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
      return {'status': 'error', 'message': 'Work order not found'}
    return {'status': 'success',
            'notes': make_serializable(row.get('notes')) or '',
            'so_notes': make_serializable(row.get('so_notes')) or '',
            'so_level_notes': make_serializable(row.get('so_level_notes')) or ''}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# UPDATE WORK ORDER STATUS
# =============================================================================

def _current_actor():
  """Best-effort current user for audit rows. Returns a display name/email when
  the Anvil Users service is enabled, else a clear fallback."""
  try:
    import anvil.users
    u = anvil.users.get_user()
    if u:
      try:
        return (u['name'] or u['email'] or 'Unknown')[:150]
      except Exception:
        return str(u)[:150]
  except Exception:
    pass
  return 'Unknown (no user session)'


def log_wo_cancel(cursor, work_order_id, wo_number, so_number, so_rel,
                  old_status, source, actor):
  """Insert one cancel-audit row into dbo.work_order_action_log. Best-effort:
  callers wrap this in try/except so a missing table never blocks a cancel."""
  cursor.execute("""
      INSERT INTO dbo.work_order_action_log
        (work_order_id, work_order_number, sales_order_number, sales_order_rel,
         action, source, old_status, new_status, actor)
      VALUES (%s, %s, %s, %s, 'Cancel', %s, %s, 'Cancelled', %s)
  """, (work_order_id, wo_number, so_number, so_rel, source, old_status, actor))


@anvil.server.callable
@perf_probe.timed
def update_work_order_status(work_order_id, new_status, completed_by=None):
  """
    Update a work order's status (generic setter).
    Sets completed_at and completed_by ONLY when marking Complete.
    
    Used by the office form for Release (-> 'Released') and Cancel
    (-> 'Cancelled'); the SE Department app owns In Progress/Paused/Complete
    via record_wo_event(). 'Cancelled' is terminal and intentionally does NOT
    stamp completion fields.
    
    Args:
        work_order_id: int - the work order to update
        new_status:    any valid status, e.g. 'Released', 'Cancelled',
                       'In Progress', 'Complete'
        completed_by:  who completed it (defaults to 'Production')
    
    Returns:
        dict with 'status': 'success' or 'error'

    v2.0: 'Released' is not set here any more. It delegates to
    routing_desk.release_work_order, the one guarded path (routing must
    exist; first group -> Ready; texts; audit). Kept as a callable name so
    the old sku_details editor flow keeps working until it is retired.
  """
  if new_status == 'Released':
    return _desk.release_work_order(work_order_id, actor=None, source='legacy')

  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Capture prior status + identifiers for cancel auditing.
    _prior = _wo_num = _so_num = _so_rel = None
    if new_status == 'Cancelled':
      cursor.execute(
        "SELECT status, work_order_number, sales_order_number, sales_order_rel "
        "FROM dbo.work_orders WHERE work_order_id = %s", (work_order_id,))
      _r = cursor.fetchone()
      if _r:
        _prior = _r['status']; _wo_num = _r['work_order_number']
        _so_num = _r['sales_order_number']; _so_rel = _r['sales_order_rel']

    if new_status == 'Complete':
      cursor.execute("""
          UPDATE dbo.work_orders
          SET status = %s,
              completed_at = GETDATE(),
              completed_by = %s
          WHERE work_order_id = %s
      """, (new_status, completed_by or 'Production', work_order_id))
    else:
      cursor.execute("""
          UPDATE dbo.work_orders
          SET status = %s
          WHERE work_order_id = %s
      """, (new_status, work_order_id))

    # Audit single-WO cancels (best-effort; never block the cancel).
    if new_status == 'Cancelled':
      try:
        log_wo_cancel(cursor, work_order_id, _wo_num, _so_num, _so_rel,
                      _prior, 'single', _current_actor())
      except Exception:
        pass

    # A WO closed here (Cancel, or an office Complete) takes its routing
    # with it: open steps are Skipped in the same transaction (step 4) -
    # one round trip, dbo.sp_pc_close_wo_routing (migration 084).
    steps_skipped = 0
    if new_status in ('Complete', 'Cancelled'):
      cursor.execute("""
          EXEC dbo.sp_pc_close_wo_routing @work_order_id=%s, @actor=%s, @note=%s
      """, (work_order_id,
            (completed_by or _current_actor() or 'Production')[:50],
            f'PC {new_status.lower()} - routing closed with the work order'))
      proc = cursor.fetchone() or {}
      if proc.get('status') != 'success':
        conn.rollback()
        conn.close()
        return {'status': 'error',
                'message': proc.get('message', 'Routing close-out failed')}
      steps_skipped = int(proc.get('steps_skipped') or 0)

    conn.commit()
    conn.close()
    return {'status': 'success', 'routing_steps_skipped': steps_skipped}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


def _worked_by_floor(cursor, work_order_id):
  """True when a routing step has recorded work at a department that is NOT
  auto-completed at release - i.e. a person on the floor has this order."""
  auto = _desk._auto_complete_depts(cursor, {})
  cursor.execute("""
      SELECT d.dept_code FROM dbo.pc_wo_routing_steps s
      JOIN dbo.pc_departments d ON d.department_id = s.department_id
      WHERE s.work_order_id = %s
        AND (s.status IN ('In Progress', 'Complete') OR s.qty_completed > 0)
  """, (work_order_id,))
  return any((r['dept_code'] or '').upper() not in auto for r in cursor.fetchall())


@anvil.server.callable
@perf_probe.timed
def unrelease_work_order(work_order_id, unreleased_by='Production'):
  """
    Reverse a Release: move a work order from 'Released' back to 'Created'.

    UnRelease is the strict inverse of Release and is allowed ONLY from
    'Released'. A WO the SE Department has already picked up (Assigned /
    In Progress / Paused) must NOT be pulled back here -- the supervisor
    unassigns it first (Assigned -> Released), after which the office may
    UnRelease it. The status guard lives in the WHERE clause so a stale
    client cannot un-release a WO the floor just advanced.

    Args:
        work_order_id:  int - the work order to un-release
        unreleased_by:  str - who performed it (audit / notification only)

    Returns:
        dict with 'status': 'success' or 'error' (+ 'message')
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
      "SELECT work_order_id, work_order_number, sales_order_number, sales_order_rel, status "
      "FROM dbo.work_orders WHERE work_order_id = %s",
      (work_order_id,))
    row = cursor.fetchone()
    if not row:
      conn.close()
      return {'status': 'error', 'message': 'Work order not found'}

    current = row['status']
    if current != 'Released':
      conn.close()
      return {
        'status': 'error',
        'message': (f"Only 'Released' work orders can be un-released "
                    f"(this one is '{current}'). If it is on the floor, "
                    f"have the SE Department unassign it first.")
      }

    # Guard: a step somebody has started or completed means the floor has
    # this work order; pulling it back would contradict the routing. Steps
    # at a department that is auto-completed at release (no board yet -
    # global_parameters pc_auto_complete_*, 2.0.1) are not "the floor".
    if _worked_by_floor(cursor, work_order_id):
      conn.close()
      return {
        'status': 'error',
        'message': ('A routing step on this work order already has recorded '
                    'work. It cannot be un-released; use Reset for Rework instead.')
      }

    cursor.execute("""
        UPDATE dbo.work_orders
        SET status = 'Created'
        WHERE work_order_id = %s
          AND status = 'Released'
    """, (work_order_id,))
    affected = cursor.rowcount
    if affected == 0:
      conn.rollback()
      conn.close()
      return {
        'status': 'error',
        'message': ('Work order changed status before it could be '
                    'un-released. Refresh and try again.')
      }

    # v2.0 (F1): the routing snapshot is kept, but its Ready step(s) go back
    # to Pending so no department queue shows an un-released work order.
    actor = (unreleased_by or _current_actor() or 'Production')[:50]
    demoted = _desk.demote_ready_steps(cursor, work_order_id, actor, 'un-released')
    _desk._log_action(cursor, row, 'UnRelease', 'single', 'Released', 'Created', actor)

    conn.commit()
    conn.close()
    return {'status': 'success', 'steps_demoted': demoted}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# MARK WORK ORDER PRINTED
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def mark_work_order_printed(work_order_id):
  """
    Update the printed_at timestamp when a work order is printed.
    
    Args:
        work_order_id: int - the work order to mark as printed
    
    Returns:
        dict with 'status': 'success' or 'error'
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE dbo.work_orders
        SET printed_at = GETDATE()
        WHERE work_order_id = %s
    """, (work_order_id,))

    conn.commit()
    conn.close()
    return {'status': 'success'}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}

# =============================================================================
# GET WORK ORDER CHANGES
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def get_work_order_changes(work_order_id):
  """
    Fetch change history for a specific work order.
    Returns changes sorted newest-first.
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            change_id,
            change_type,
            field_name,
            old_value,
            new_value,
            detected_at,
            acknowledged_at,
            acknowledged_by
        FROM dbo.work_order_changes
        WHERE work_order_id = %s
        ORDER BY detected_at DESC
    """, (work_order_id,))

    changes = [serialize_row(row) for row in cursor.fetchall()]
    conn.close()

    return {'status': 'success', 'changes': changes}

  except Exception as e:
    return {'status': 'error', 'message': str(e), 'changes': []}


# =============================================================================
# ACKNOWLEDGE WORK ORDER CHANGES
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def acknowledge_work_order_changes(work_order_id, acknowledged_by='Production'):
  """
    Mark all unacknowledged changes for a work order as reviewed.
    Resets pending_change_count to 0.
    Auto-advances Verify statuses to Complete.
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Mark all unacknowledged changes
    cursor.execute("""
            UPDATE dbo.work_order_changes
            SET acknowledged_at = GETDATE(),
                acknowledged_by = %s
            WHERE work_order_id = %s
              AND acknowledged_at IS NULL
        """, (acknowledged_by, work_order_id))

    # Reset the counter on the work order
    cursor.execute("""
            UPDATE dbo.work_orders
            SET pending_change_count = 0
            WHERE work_order_id = %s
        """, (work_order_id,))

    # Auto-advance Verify statuses to Complete
    cursor.execute("""
            UPDATE dbo.work_orders
            SET status = 'Complete',
                completed_at = GETDATE(),
                completed_by = %s
            WHERE work_order_id = %s
              AND status IN ('Shipped - Verify', 'Closed - Verify', 'Invoiced - Verify')
        """, (acknowledged_by, work_order_id))

    conn.commit()
    conn.close()
    return {'status': 'success'}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}

@anvil.server.callable
@perf_probe.timed
def update_work_order_override_qty(work_order_id, override_qty):
  """Set or clear the PM production quantity override.
    Pass None to clear and revert to e21 quantity tracking."""
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
            UPDATE dbo.work_orders
            SET wo_override_qty = %s
            WHERE work_order_id = %s
        """, (override_qty, work_order_id))
    conn.commit()
    conn.close()
    return {'status': 'success'}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# UPDATE WORK ORDER DUE DATE
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def update_work_order_due_date(work_order_id, due_date_str):
  """Set or clear the WO due date (production completion target).
    Pass an ISO date string (YYYY-MM-DD) or None to clear."""
  try:
    conn = get_connection()
    cursor = conn.cursor()

    if due_date_str:
      cursor.execute("""
            UPDATE dbo.work_orders
            SET wo_due_date = CAST(%s AS DATE)
            WHERE work_order_id = %s
        """, (due_date_str, work_order_id))
    else:
      cursor.execute("""
            UPDATE dbo.work_orders
            SET wo_due_date = NULL
            WHERE work_order_id = %s
        """, (work_order_id,))

    conn.commit()
    conn.close()
    return {'status': 'success'}
  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# BULK ACKNOWLEDGE ORPHANED VERIFY-STATUS WORK ORDERS
# (Temporary — testing phase convenience)
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def bulk_acknowledge_verify_work_orders(acknowledged_by='Production'):
  """
  Acknowledge and complete ALL work orders in Verify/Cancelled statuses.

  This is a testing-phase convenience function. During testing, the
  production team is not issuing all work orders, so EasyMorph's
  change detection creates orphaned WOs in Shipped/Invoiced/Closed-Verify
  or Cancelled. This bulk-clears them so the team can focus on real work orders.

  2026-09-23: the work is done by dbo.sp_pc_accept_orphans (migration 084)
  in ONE call and ONE transaction - acknowledge pending changes, zero the
  counters, Skip every open routing step on the target WOs (wo-status-
  ownership step 4), advance the Verify statuses to Complete (Cancelled
  stays Cancelled). The 09-17 Python version looped per open step and hit
  Anvil's 30 s server-call limit once the orphan set grew (1,145 WOs / 228
  open steps on 09-23); a timeout is not a caught SQL error, so the form
  crashed. Now the volume is the database's problem, not the call's.

  Returns:
    dict with status, count of acknowledged WOs, breakdown by status,
    routing_steps_skipped, routing_wos_touched
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("EXEC dbo.sp_pc_accept_orphans @acknowledged_by=%s",
                   ((acknowledged_by or 'Production')[:50],))
    proc = cursor.fetchone() or {}
    if proc.get('status') != 'success':
      conn.rollback()
      conn.close()
      return {'status': 'error',
              'message': proc.get('message', 'Accept Orphans failed')}

    conn.commit()
    conn.close()

    breakdown = {label: int(proc.get(key) or 0)
                 for label, key in (('Shipped - Verify', 'shipped_verify'),
                                    ('Invoiced - Verify', 'invoiced_verify'),
                                    ('Closed - Verify', 'closed_verify'),
                                    ('Cancelled', 'cancelled'))
                 if int(proc.get(key) or 0) > 0}
    total = int(proc.get('acknowledged') or 0)
    steps_skipped = int(proc.get('routing_steps_skipped') or 0)
    wos_touched = int(proc.get('routing_wos_touched') or 0)

    if total == 0:
      return {
        'status': 'success',
        'acknowledged': 0,
        'breakdown': {},
        'routing_steps_skipped': 0,
        'routing_wos_touched': 0,
        'message': proc.get('message') or 'No work orders in Verify or Cancelled status'
      }

    # Build summary message
    parts = [f"{cnt} {status}" for status, cnt in breakdown.items()]
    detail = ", ".join(parts)
    if steps_skipped:
      detail += (f"; skipped {steps_skipped} open routing step"
                 f"{'s' if steps_skipped != 1 else ''} on {wos_touched} of them")

    return {
      'status': 'success',
      'acknowledged': total,
      'breakdown': breakdown,
      'routing_steps_skipped': steps_skipped,
      'routing_wos_touched': wos_touched,
      'message': f"Acknowledged {total} orphaned work orders ({detail})"
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# SALES BACKORDER DETAIL
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def get_sales_backorder_detail(sales_order_number):
  """
  Fetch all sales backorder rows for a given sales order number.

  Returns line items from the sales_backorders table showing the current
  state of each line item across backorder releases — max release,
  current quantity, balance due, total shipped, and item status.

  Args:
    sales_order_number: str — the sales order number (without release)

  Returns:
    dict with status, and list of backorder rows
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
      SELECT
        sb.nx_order_numb AS sales_order_number,
        sb.nx_max_rel_numb AS max_release,
        sb.nx_item_no AS item_number,
        sb.nx_sku AS sku,
        sb.nx_qty AS quantity,
        sb.nx_item_status AS item_status,
        sb.nx_baldue AS balance_due,
        sb.nx_tot_qty_ship AS total_qty_shipped
      FROM dbo.sales_backorders sb
      WHERE sb.nx_order_numb = %s
      ORDER BY sb.nx_item_no
    """, (sales_order_number,))

    rows = cursor.fetchall()

    # Carrier / MOS for the order header (Carrier+MOS feature). Sourced from
    # sales_orders_due; prefer a non-empty value from the most recent release.
    cursor.execute("""
      SELECT TOP 1 nx_carrier, nx_mos
      FROM dbo.sales_orders_due
      WHERE nx_order_numb = %s
      ORDER BY
        CASE WHEN LTRIM(RTRIM(ISNULL(nx_carrier, ''))) <> '' THEN 0 ELSE 1 END,
        nx_rel_numb DESC, nx_item_no ASC
    """, (sales_order_number,))
    _crow = cursor.fetchone()
    carrier = (_crow['nx_carrier'] or '').strip() if _crow else ''
    mos = (_crow['nx_mos'] or '').strip() if _crow else ''

    conn.close()

    backorders = []
    for row in rows:
      serialized = serialize_row(row)
      # Format item_status for display
      status_code = serialized.get('item_status')
      if status_code is not None:
        try:
          code = int(float(str(status_code).strip()))
          status_map = {
            0: 'Hold',
            5: 'Regulatory Hold',
            10: 'Entered',
            12: 'Committed',
            14: 'Allocated',
            15: 'Material Received',
            16: 'Shipped',
            18: 'Invoiced',
            90: 'Canceled',
          }
          serialized['item_status_display'] = status_map.get(code, str(code))
        except (ValueError, TypeError):
          serialized['item_status_display'] = str(status_code)
      else:
        serialized['item_status_display'] = ''

      # Clean up numeric formatting
      for field in ('quantity', 'balance_due', 'total_qty_shipped'):
        val = serialized.get(field)
        if val is not None:
          try:
            serialized[field] = int(float(val))
          except (ValueError, TypeError):
            pass

      backorders.append(serialized)

    return {
      'status': 'success',
      'backorders': backorders,
      'carrier': carrier,
      'mos': mos
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }