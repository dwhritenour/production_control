"""
Work Order Auto-Creation - Anvil Server Module
================================================
Scans sales_orders_due for special straining element configurations
and automatically creates work orders when ordered config differs
from the standard default.

Trigger:
    - On-demand via UI button ("Scan New Work Orders")
    - Scheduled via EasyMorph (every 15 minutes with sales_orders_due refresh)

Logic:
    1. Find sales order lines with attributes (nx_attrib_val IS NOT NULL)
    2. Compare ordered config vs. standard default (attrib_val_default)
    3. Normalize whitespace for reliable comparison
    4. Skip lines that already have a work order
    5. Generate WO-YYYY-NNNN numbers
    6. Insert new work orders

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: February 2026
"""

import anvil.server
from datetime import datetime

# Import centralized configuration
from . import db_config


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_connection():
  """Get database connection from centralized config."""
  return db_config.get_nexus_connection()


def normalize_attrib(value):
  """
    Normalize attribute values for reliable comparison.
    Strips whitespace, collapses multiple spaces, and standardizes
    pipe delimiters to 'VALUE | VALUE' format.
  """
  if not value:
    return ''
  # Strip outer whitespace
  val = value.strip()
  # Split on pipe, strip each part, rejoin with single-space pipe
  parts = [p.strip() for p in val.split('|')]
  # Remove empty trailing parts (e.g., "CS | " → ["CS", ""])
  while parts and parts[-1] == '':
    parts.pop()
  return ' | '.join(parts)


def get_next_wo_number(cursor):
  """
    Generate the next work order number in WO-YYYY-NNNN format.
    Queries the highest existing number for the current year
    and increments by 1.
  """
  year = datetime.now().year
  prefix = f"WO-{year}-"

  cursor.execute("""
      SELECT TOP 1 work_order_number
      FROM dbo.work_orders
      WHERE work_order_number LIKE %s
      ORDER BY work_order_number DESC
  """, (f"{prefix}%",))

  row = cursor.fetchone()
  if row:
    # Extract the sequence number from the last WO
    last_num = row['work_order_number']
    try:
      seq = int(last_num.split('-')[-1]) + 1
    except (ValueError, IndexError):
      seq = 1
  else:
    seq = 1

  return f"{prefix}{seq:04d}"





# =============================================================================
# ANVIL CALLABLE
# =============================================================================

@anvil.server.callable
def scan_and_create_work_orders():
  """
    Call the stored procedure to scan for special configs
    and create work orders. All heavy lifting happens in SQL Server.
  """
  try:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("EXEC dbo.sp_create_work_orders")
    row = cursor.fetchone()
    created = row['work_orders_created'] if row else 0
    conn.commit()

    # 056 (2026-09-08): a work order born AFTER today's plan was published
    # still gets the plan stamp. The procedure is a no-op when today's plan
    # is not published, and is skipped entirely before 056 is applied.
    if created > 0:
      try:
        cursor.execute("SELECT OBJECT_ID('dbo.sp_stamp_plan_work_orders') AS o")
        if (cursor.fetchone() or {}).get('o') is not None:
          cursor.execute("EXEC dbo.sp_stamp_plan_work_orders @plan_date = NULL")
          cursor.fetchone()
          conn.commit()
      except Exception:
        conn.rollback()          # stamping is best effort; creation stands
    conn.close()

    if created > 0:
      message = f"Created {created} new work order(s)."
    else:
      message = "No new special configurations found."

    return {
      'status': 'success',
      'created': created,
      'errors': [],
      'message': message
    }

  except Exception as e:
    return {
      'status': 'error',
      'created': 0,
      'errors': [str(e)],
      'message': f'Scan failed: {str(e)}'
    }