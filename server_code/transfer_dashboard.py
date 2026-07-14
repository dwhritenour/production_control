"""
Anvil Server Module - Transfer Dashboard
=========================================
Server-side code for the Titan Flow Control Transfer Dashboard Anvil app.

This module provides database connectivity and business logic for:
    - Fetching pending transfer recommendations
    - Updating decisions (Approve/Reject) and quantities
    - Applying approved transfers and generating CSV
    - Logging actions to audit trail

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: January 2026
"""

import anvil.email
import anvil.server
import anvil.media
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config
from .db_config import TRANSFER_CONSTANTS  # noqa: F401  (kept for future use)

# Pick List PDF generator (temporary replacement for the CSV export;
# will be superseded once the e21 tie-in is in place)
from .pick_list_pdf_generator import generate_pick_list_pdf


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


def _to_int(v):
  """Coerce a numeric value to int; 0 on failure."""
  try:
    return int(round(float(v)))
  except (TypeError, ValueError):
    return 0


def _fmt_int(v):
  """Render a numeric demand/qty value as a clean integer string."""
  return str(_to_int(v))


def _attach_demand_detail(conn, rows):
  """v5.21: Attach demand-bucket + open sales-order strings to each pending row.

  Additive and DEFENSIVE: any failure leaves rows untouched so the grid still
  loads. Two read-only fields are added to every row dict:
    - demand_buckets : weekly 0-7 / 8-14 / 15-21 / 22-28 day increments,
                       read straight from dbo.vw_sku_demand_buckets.
    - sales_orders   : open order list 'number-rel:qty', soonest ship first,
                       within the 28-day window, from dbo.sales_orders_due.
  """
  if not rows:
    return
  skus = sorted({r['sku'] for r in rows if r.get('sku')})
  if not skus:
    return
  try:
    placeholders = ','.join(['%s'] * len(skus))
    cur = conn.cursor()

    # Weekly demand buckets (one row per SKU) from the view
    cur.execute(
      "SELECT sku, week_1_incremental, week_2_incremental, "
      "week_3_incremental, week_4_incremental "
      "FROM dbo.vw_sku_demand_buckets WHERE sku IN (" + placeholders + ")",
      tuple(skus))
    buckets = {}
    for b in cur.fetchall():
      w1 = _to_int(b['week_1_incremental'])
      w2 = _to_int(b['week_2_incremental'])
      w3 = _to_int(b['week_3_incremental'])
      w4 = _to_int(b['week_4_incremental'])
      buckets[b['sku']] = (
        "0-7d: " + str(w1) +
        " | 8-14d: " + str(w2) +
        " | 15-21d: " + str(w3) +
        " | 22-28d: " + str(w4) +
        " | Total: " + str(w1 + w2 + w3 + w4))

    # Open sales orders (balance due) within 28 days, soonest ship first
    cur.execute(
      "SELECT nx_sku, nx_order_numb, nx_rel_numb, nx_qty_balance_due "
      "FROM dbo.sales_orders_due "
      "WHERE nx_qty_balance_due > 0 "
      "AND DATEDIFF(day, CAST(GETDATE() AS DATE), nx_dship_by_date) <= 28 "
      "AND nx_sku IN (" + placeholders + ") "
      "ORDER BY nx_sku, nx_dship_by_date ASC",
      tuple(skus))
    orders = {}
    for o in cur.fetchall():
      label = str(o['nx_order_numb'])
      if o.get('nx_rel_numb'):
        label = label + "-" + str(o['nx_rel_numb'])
      orders.setdefault(o['nx_sku'], []).append(
        label + ":" + _fmt_int(o['nx_qty_balance_due']))

    for r in rows:
      sku = r.get('sku')
      r['demand_buckets'] = buckets.get(sku, 'No demand in 28-day horizon')
      ord_list = orders.get(sku, [])
      r['sales_orders'] = ', '.join(ord_list) if ord_list else 'No open sales orders'
  except Exception as e:
    # Non-fatal: demand detail is a display nicety, never block the grid
    print("demand-detail enrichment skipped: " + str(e))



# =============================================================================
# DATA RETRIEVAL FUNCTIONS
# =============================================================================

@anvil.server.callable
def get_pending_transfers():
  """
    Retrieve all pending transfer recommendations for the DataGrid.
    
    Returns:
        dict: {
            'status': 'success' or 'error',
            'rows': list of transfer recommendation dicts,
            'count': number of rows,
            'message': error message if applicable
        }
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT 
            recommendation_id,
            sku,
            event_type,
            inventory_urgency,
            time_urgency,
            starlite_available,
            starlite_on_hand,
            starlite_effective_on_hand,   -- v5.6: snapshot of virtual Starlite inventory at rec time
            hub_available,
            hub_on_hand,
            hub_effective_on_hand,        -- v5.6: snapshot of virtual Hub inventory at rec time
            min_stock,
            max_stock,
            total_demand,
            order_count,
            earliest_ship_date,
            latest_ship_date,
            calculated_transfer_qty,
            ai_recommended_qty,
            manager_override_qty,
            confidence_level,
            reasoning,
            risk_factors,
            seasonal_consideration,
            status,
            batch_id,
            recommendation_date,
            notes,
            paused_date,
            DATEDIFF(day, paused_date, GETDATE()) AS days_paused,
            ISNULL(has_duplicate_warning, 0) AS has_duplicate_warning
        FROM dbo.ai_transfer_recommendations
        WHERE status IN ('Pending', 'Paused')
        ORDER BY
            CASE WHEN ISNULL(has_duplicate_warning, 0) = 1 THEN 1 ELSE 2 END,  -- v5.5: duplicate warnings first
            CASE status WHEN 'Paused' THEN 1 ELSE 2 END,                       -- then Paused items
            CASE inventory_urgency
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'MEDIUM' THEN 3
                WHEN 'LOW' THEN 4
                ELSE 5
            END,
            event_type DESC,
            sku
    """

    cursor.execute(query)
    rows = [serialize_row(row) for row in cursor.fetchall()]

    _attach_demand_detail(conn, rows)

    conn.close()

    return {
      'status': 'success',
      'rows': rows,
      'count': len(rows),
      'message': f'Retrieved {len(rows)} pending transfers'
    }

  except Exception as e:
    return {
      'status': 'error',
      'rows': [],
      'count': 0,
      'message': str(e)
    }


@anvil.server.callable
def get_transfer_stats():
  """
    Get summary statistics for the dashboard header.
    
    Returns:
        dict: Counts by status and urgency
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Status counts
    cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM dbo.ai_transfer_recommendations
            WHERE recommendation_date >= DATEADD(day, -7, GETDATE())
            GROUP BY status
        """)
    status_counts = {row['status']: row['count'] for row in cursor.fetchall()}

    # Urgency counts for pending only
    cursor.execute("""
            SELECT inventory_urgency, COUNT(*) as count
            FROM dbo.ai_transfer_recommendations
            WHERE status = 'Pending'
            GROUP BY inventory_urgency
        """)
    urgency_counts = {row['inventory_urgency']: row['count'] for row in cursor.fetchall()}

    conn.close()

    return {
      'status': 'success',
      'by_status': status_counts,
      'by_urgency': urgency_counts,
      'pending': status_counts.get('Pending', 0),
      'approved': status_counts.get('Approved', 0),
      'rejected': status_counts.get('Rejected', 0),
      'applied': status_counts.get('Applied', 0)
    }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


# =============================================================================
# UPDATE FUNCTIONS
# =============================================================================

@anvil.server.callable
def update_decision(recommendation_id, decision, transfer_qty=None, notes=None, pause_reason=None, reviewed_by='Hub Manager'):
  """
    Update the decision for a single transfer recommendation.
    
    Args:
        recommendation_id: The ID of the recommendation to update
        decision: 'Approved', 'Rejected', 'Paused', or 'Pending'
        transfer_qty: Optional override quantity (saved to manager_override_qty)
        notes: Optional notes from reviewer
        pause_reason: Reason for pausing (required if decision='Paused')
        reviewed_by: Who made the decision
    
    Returns:
        dict: {status, message}
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # Handle Paused status separately (needs paused_date and pause_reason)
    if decision == 'Paused':
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = 'Paused',
                    paused_date = GETDATE(),
                    pause_reason = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
            """
      cursor.execute(query, (pause_reason, notes, reviewed_by, recommendation_id))

    # Handle Approved/Rejected with optional quantity override
    elif transfer_qty is not None:
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = %s,
                    manager_override_qty = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
            """
      cursor.execute(query, (decision, transfer_qty, notes, reviewed_by, recommendation_id))

    else:
      query = """
                UPDATE dbo.ai_transfer_recommendations
                SET status = %s,
                    notes = %s,
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id = %s
            """
      cursor.execute(query, (decision, notes, reviewed_by, recommendation_id))

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
      return {
        'status': 'success',
        'message': f'Updated recommendation {recommendation_id} to {decision}'
      }
    else:
      return {
        'status': 'error',
        'message': f'Recommendation {recommendation_id} not found'
      }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }


@anvil.server.callable
def bulk_update_decisions(updates):
  """
    Bulk update recommendation decisions.
    Handles Approved, Rejected, and Paused statuses.
    """
  if not updates:
    return {'status': 'error', 'message': 'No updates provided'}

  conn = get_connection()
  updated_count = 0

  try:
    cursor = conn.cursor()

    for update in updates:
      decision = update['decision']
      rec_id = update['recommendation_id']

      if decision == 'Approved':
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Approved',
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE(),
                        manager_override_qty = %s
                    WHERE recommendation_id = %s
                """, (update.get('transfer_qty'), rec_id))

      elif decision == 'Rejected':
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Rejected',
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE()
                    WHERE recommendation_id = %s
                """, (rec_id,))

      elif decision == 'Paused':
        cursor.execute("""
                    UPDATE dbo.ai_transfer_recommendations
                    SET status = 'Paused',
                        paused_date = GETDATE(),
                        pause_reason = %s,
                        reviewed_by = 'Hub_Manager',
                        reviewed_date = GETDATE()
                    WHERE recommendation_id = %s
                """, (update.get('pause_reason'), rec_id))

      updated_count += cursor.rowcount

    conn.commit()
    return {'status': 'success', 'updated_count': updated_count}

  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': str(e)}

  finally:
    conn.close()


# =============================================================================
# APPLY TRANSFERS - Generate Pick List PDF
# =============================================================================
# NOTE (2026-04): This path used to emit a CSV for TGI to use as an
# e21 import file. TGI has not delivered the e21 tie-in, so the workflow
# has been temporarily switched to a printable Pick List PDF that matches
# the style of the Transfer Trip Assignment Sheet. The return payload
# exposes the PDF under both 'pdf_media' and 'csv_media' so existing
# clients that still reference the old key continue to function.
# =============================================================================

@anvil.server.callable
def apply_transfers(reviewed_by='Hub Manager'):
  """
    Apply all approved transfers:
    1. Fetch all Approved recommendations
    2. Update status to 'Applied'
    3. Log to ai_agent_log
    4. Generate a Pick List PDF for the applied items
    5. Return the PDF as downloadable media

    Returns:
        dict: {
            'status': 'success' or 'error',
            'pdf_media': Anvil media object for download (the Pick List),
            'csv_media': alias of 'pdf_media' for backward compatibility,
            'applied_count': number of transfers applied,
            'total_qty': total units transferred,
            'filename': name of the PDF,
            'message': summary message
        }
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Get all approved recommendations
    cursor.execute("""
            SELECT
                r.recommendation_id,
                r.sku,
                r.manager_override_qty,
                r.ai_recommended_qty,
                r.calculated_transfer_qty
            FROM dbo.ai_transfer_recommendations r
            WHERE r.status = 'Approved'
            ORDER BY r.event_type DESC, r.sku
        """)

    approved = list(cursor.fetchall())

    if not approved:
      conn.close()
      return {
        'status': 'success',
        'pdf_media': None,
        'csv_media': None,
        'applied_count': 0,
        'total_qty': 0,
        'filename': None,
        'message': 'No approved transfers to apply'
      }

    # 2. Determine which recommendations actually have a positive qty
    total_qty = 0
    applied_ids = []
    for rec in approved:
      qty = int(rec['manager_override_qty']
                or rec['ai_recommended_qty']
                or rec['calculated_transfer_qty']
                or 0)
      if qty > 0:
        total_qty += qty
        applied_ids.append(rec['recommendation_id'])

    # 3. Mark them Applied
    if applied_ids:
      placeholders = ','.join(['%s'] * len(applied_ids))
      cursor.execute(f"""
                UPDATE dbo.ai_transfer_recommendations
                SET status = 'Applied',
                    reviewed_by = %s,
                    reviewed_date = GETDATE()
                WHERE recommendation_id IN ({placeholders})
            """, (reviewed_by,) + tuple(applied_ids))

    # 4. Log to ai_agent_log
    cursor.execute("""
            INSERT INTO dbo.ai_agent_log (
                action_type, sku, old_value, new_value, reason,
                action_timestamp, executed_by
            )
            VALUES (%s, %s, %s, %s, %s, GETDATE(), %s)
        """, (
      'batch_transfer_apply',
      None,
      str(len(applied_ids)),
      f'Applied {len(applied_ids)} transfers, {total_qty} units',
      f'Applied via Anvil Dashboard by {reviewed_by} (Pick List PDF)',
      'anvil_transfer_dashboard'
    ))

    conn.commit()
    conn.close()

    # 5. Generate Pick List PDF for the items we just Applied
    pdf_media = generate_pick_list_pdf(applied_ids) if applied_ids else None

    return {
      'status': 'success',
      'pdf_media': pdf_media,
      'csv_media': pdf_media,     # legacy alias — same media object
      'applied_count': len(applied_ids),
      'total_qty': total_qty,
      'filename': pdf_media.name if pdf_media else None,
      'message': f'Applied {len(applied_ids)} transfers ({total_qty} total units)'
    }

  except Exception as e:
    return {
      'status': 'error',
      'pdf_media': None,
      'csv_media': None,
      'applied_count': 0,
      'total_qty': 0,
      'filename': None,
      'message': str(e)
    }


@anvil.server.callable
def reject_pending():
  """
    Mark all remaining Pending items as Rejected.
    Useful for clearing the queue after review.
    
    Returns:
        dict: {status, rejected_count, message}
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
            UPDATE dbo.ai_transfer_recommendations
            SET status = 'Rejected',
                reviewed_by = 'Hub Manager',
                reviewed_date = GETDATE(),
                notes = 'Bulk rejected via dashboard'
            WHERE status = 'Pending'
        """)

    affected = cursor.rowcount
    conn.commit()
    conn.close()

    return {
      'status': 'success',
      'rejected_count': affected,
      'message': f'Rejected {affected} pending transfers'
    }

  except Exception as e:
    return {
      'status': 'error',
      'rejected_count': 0,
      'message': str(e)
    }


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

@anvil.server.callable
def test_connection():
  """
    Test database connectivity.
    Use this to verify the connection is working.
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 5 * FROM ai_transfer_recommendations WHERE status = 'Pending'")
    rows = [serialize_row(row) for row in cursor.fetchall()]
    conn.close()
    return {"status": "success", "rows": rows, "count": len(rows)}
  except Exception as e:
    return {"status": "error", "message": str(e)}


@anvil.server.callable
def get_recommendation_detail(recommendation_id):
  """
    Get full details for a single recommendation.
    Useful for a detail/popup view.
    """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
            SELECT *
            FROM dbo.ai_transfer_recommendations
            WHERE recommendation_id = %s
        """, (recommendation_id,))

    row = cursor.fetchone()
    conn.close()

    if row:
      return {
        'status': 'success',
        'recommendation': serialize_row(row)
      }
    else:
      return {
        'status': 'error',
        'message': f'Recommendation {recommendation_id} not found'
      }

  except Exception as e:
    return {
      'status': 'error',
      'message': str(e)
    }