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
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config
from . import sales_orders_due_dashboard as _sod  # rush-method whitelist (single source)


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


def _has_plan_columns(cursor):
  """True once 056 is applied. Lets this module be pasted before the
  migration runs without breaking the grid."""
  cursor.execute("SELECT COL_LENGTH('dbo.work_orders', 'plan_date') AS c")
  row = cursor.fetchone()
  return bool(row and row.get('c') is not None)


def _has_desc_column(cursor):
  """True once 058 (work_orders.sku_desc) is applied."""
  cursor.execute("SELECT COL_LENGTH('dbo.work_orders', 'sku_desc') AS c")
  row = cursor.fetchone()
  return bool(row and row.get('c') is not None)


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
def get_work_orders(status_filter=None, search_text=None, active_only=False, backorder_only=False, statuses=None, show_rush_only=False, limit=4000):
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
            wo.sales_order_number,
            wo.sales_order_rel,
            wo.sales_order_line,
            wo.part_number,
            wo.part_attribute,
            wo.attribute_value,
            wo.attribute_desc,
            wo.quantity,
            wo.wo_override_qty,
            wo.notes,
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
            COALESCE(so.nx_notes, wo.notes)              AS so_notes,
            COALESCE(so.nx_so_notes, wo.so_notes)        AS so_level_notes, 
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

      # Carrier / MOS / rush flag (Carrier+MOS+Rush feature)
      serialized['nx_carrier'] = (serialized.get('nx_carrier') or '').strip()
      serialized['nx_mos']     = (serialized.get('nx_mos') or '').strip()
      serialized['is_rush']    = bool(serialized.get('is_rush'))

      work_orders.append(serialized)

    # -----------------------------------------------------------------
    # Summary counts (always unfiltered for the cards)
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT status, COUNT(*) AS cnt
        FROM dbo.work_orders
        GROUP BY status
    """)
    counts = {row['status']: row['cnt'] for row in cursor.fetchall()}

    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM dbo.work_orders
        WHERE pending_change_count > 0
          AND status NOT IN ('Complete')
    """)
    row = cursor.fetchone()
    counts['PendingChanges'] = row['cnt'] if row else 0

    # Rush count: live-status work orders whose linked SO is a rush ship method.
    cursor.execute(f"""
        SELECT COUNT(*) AS cnt
        FROM dbo.work_orders wo
        LEFT JOIN dbo.sales_orders_due so
            ON wo.sales_order_number = so.nx_order_numb
           AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
           AND wo.sales_order_rel = so.nx_rel_numb
        WHERE wo.status IN ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')
          AND {_sod._rush_bool_sql('so.')}
    """)
    _rr = cursor.fetchone()
    counts['rush'] = _rr['cnt'] if _rr else 0

    # 056: today's plan — how many work orders, how many already released
    # (anything past 'Created'). Drives the 'Plan: 12/42 released' chip.
    counts['plan_today'] = 0
    counts['plan_released'] = 0
    if has_plan:
      cursor.execute(f"""
          SELECT COUNT(*) AS cnt,
                 SUM(CASE WHEN status <> 'Created' THEN 1 ELSE 0 END) AS released
          FROM dbo.work_orders
          WHERE plan_date = CAST(GETDATE() AS DATE)
            AND status NOT IN ({", ".join(["%s"] * len(_WO_TERMINAL))})
      """, _WO_TERMINAL)
      _pr = cursor.fetchone()
      if _pr:
        counts['plan_today'] = _pr['cnt'] or 0
        counts['plan_released'] = _pr['released'] or 0

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
  """
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

    conn.commit()
    conn.close()
    return {'status': 'success'}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


@anvil.server.callable
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
      "SELECT status FROM dbo.work_orders WHERE work_order_id = %s",
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

    cursor.execute("""
        UPDATE dbo.work_orders
        SET status = 'Created'
        WHERE work_order_id = %s
          AND status = 'Released'
    """, (work_order_id,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
      return {
        'status': 'error',
        'message': ('Work order changed status before it could be '
                    'un-released. Refresh and try again.')
      }
    return {'status': 'success'}

  except Exception as e:
    return {'status': 'error', 'message': str(e)}


# =============================================================================
# MARK WORK ORDER PRINTED
# =============================================================================

@anvil.server.callable
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
def bulk_acknowledge_verify_work_orders(acknowledged_by='Production'):
  """
  Acknowledge and complete ALL work orders in Verify/Cancelled statuses.

  This is a testing-phase convenience function. During testing, the
  production team is not issuing all work orders, so EasyMorph's
  change detection creates orphaned WOs in Shipped/Invoiced/Closed-Verify
  or Cancelled. This bulk-clears them so the team can focus on real work orders.

  Steps per WO:
    1. Mark all unacknowledged changes as reviewed
    2. Reset pending_change_count to 0
    3. Advance Verify statuses to Complete (Cancelled stays Cancelled)

  Returns:
    dict with status, count of acknowledged WOs, and breakdown by status
  """
  target_statuses = (
    'Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify', 'Cancelled'
  )

  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Count by status for reporting
    cursor.execute("""
      SELECT status, COUNT(*) AS cnt
      FROM dbo.work_orders
      WHERE status IN ('Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify', 'Cancelled')
      GROUP BY status
    """)
    breakdown = {row['status']: row['cnt'] for row in cursor.fetchall()}
    total = sum(breakdown.values())

    if total == 0:
      conn.close()
      return {
        'status': 'success',
        'acknowledged': 0,
        'breakdown': {},
        'message': 'No work orders in Verify or Cancelled status'
      }

    # 1. Mark all unacknowledged changes on these WOs as reviewed
    cursor.execute("""
      UPDATE wc
      SET wc.acknowledged_at = GETDATE(),
          wc.acknowledged_by = %s
      FROM dbo.work_order_changes wc
      INNER JOIN dbo.work_orders wo
        ON wc.work_order_id = wo.work_order_id
      WHERE wo.status IN ('Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify', 'Cancelled')
        AND wc.acknowledged_at IS NULL
    """, (acknowledged_by,))

    # 2. Reset pending_change_count
    cursor.execute("""
      UPDATE dbo.work_orders
      SET pending_change_count = 0
      WHERE status IN ('Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify', 'Cancelled')
    """)

    # 3. Advance Verify statuses to Complete (Cancelled stays as-is)
    cursor.execute("""
      UPDATE dbo.work_orders
      SET status = 'Complete',
          completed_at = GETDATE(),
          completed_by = %s
      WHERE status IN ('Shipped - Verify', 'Invoiced - Verify', 'Closed - Verify')
    """, (acknowledged_by,))

    conn.commit()
    conn.close()

    # Build summary message
    parts = [f"{cnt} {status}" for status, cnt in breakdown.items()]
    detail = ", ".join(parts)

    return {
      'status': 'success',
      'acknowledged': total,
      'breakdown': breakdown,
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