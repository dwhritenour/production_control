"""
Anvil Server Module - Part Master
==================================
Server-side code for SKU lookup and PC-created SKUs (Routing v2,
SKU Details / Routing Library form).

"Once we do something once, it is no longer special": parts that e21
only describes via free-text order notes get real SKUs, minted here.

Data rules (migration 004, applied 2026-07-18):
    - dbo.part_specs is a BULK-REFRESHED e21 mirror - never written.
    - PC-created SKUs live in dbo.pc_part_master (this module is the
      only writer).
    - ALL lookups use dbo.vw_part_master (union of both; e21 rows win
      automatically once an exported SKU lands in the mirror).

Database: SQL Server (nexus) via pymssql
Uses centralized db_config for credentials (Anvil Secrets)

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: July 2026
"""

import anvil.server
from datetime import datetime, date
from decimal import Decimal

# Import centralized configuration
from . import db_config
from . import perf_probe   # 2026-09-24 timing probe - remove with perf_probe.py


# =============================================================================
# SERIALIZATION HELPERS
# =============================================================================

def make_serializable(value):
  """Convert non-serializable types (datetime, Decimal, bytes) for Anvil."""
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


def get_connection():
  """Create and return a database connection (as_dict=True cursors)."""
  return db_config.get_nexus_connection()


def _current_actor():
  """Best-effort current user for audit rows."""
  try:
    import anvil.users
    u = anvil.users.get_user()
    if u:
      try:
        return (u['name'] or u['email'] or 'Unknown')[:50]
      except Exception:
        return str(u)[:50]
  except Exception:
    pass
  return 'Production'


def _clean_notes(raw):
  """e21 notes arrive with \\r separators and ' | ' soft wraps - normalize
  to readable multi-line text."""
  if not raw:
    return ''
  text = raw.replace('\r\n', '\n').replace('\r', '\n').replace(' | ', ' ')
  lines = [ln.strip() for ln in text.split('\n')]
  out = []
  for ln in lines:
    if ln or (out and out[-1]):   # collapse runs of blank lines to one
      out.append(ln)
  return '\n'.join(out).strip()


def _norm_attrib(v):
  """Canonical form for attribute matching: uppercase, whitespace
  collapsed. Matching keys on attribute_value ONLY (attribute_desc
  drifts from value in live e21 data)."""
  return ' '.join((v or '').upper().split())


# =============================================================================
# SKU SEARCH / DETAIL  (all via vw_part_master)
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def search_part_master(term):
  """
    SKU autocomplete for the Routing Library. Matches sku OR description.

    Returns:
        [{sku, part_desc, sku_source, series, size}]  (max 25)
  """
  term = (term or '').strip()
  if len(term) < 2:
    return []
  like = f'%{term}%'
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP 25 sku, part_desc, sku_source, series, size
        FROM dbo.vw_part_master
        WHERE sku LIKE %s OR part_desc LIKE %s
        ORDER BY sku
    """, (like, like))
    return [serialize_row(r) for r in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def get_part_detail(sku):
  """
    Full detail for one SKU + whether it has an active default routing.

    Returns:
        {status, part{...}, has_template, template_step_count}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sku, part_desc, line, series, model, body_mat,
               asme_class, end_connect, size, base_sku, attrib_value,
               sku_source
        FROM dbo.vw_part_master
        WHERE sku = %s
    """, (sku,))
    part = cursor.fetchone()
    if not part:
      return {'status': 'error', 'message': f'SKU {sku} not found'}

    cursor.execute("""
        SELECT COUNT(*) AS n
        FROM dbo.pc_routing_templates t
        JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
        WHERE t.part_number = %s AND t.attribute_value IS NULL
          AND t.is_active = 1
    """, (sku,))
    n = cursor.fetchone()['n']
    return {'status': 'success',
            'part': serialize_row(part),
            'has_template': n > 0,
            'template_step_count': n}
  finally:
    conn.close()


# =============================================================================
# CREATE PC SKU
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def create_pc_sku(payload, actor=None):
  """
    Mint a PC-created SKU (New SKU dialog).

    payload: {sku, part_desc, line, series, model, body_mat, asme_class,
              end_connect, size, base_sku, special_notes}
    sku + part_desc required. Uniqueness enforced across BOTH tables
    (this module is the gatekeeper - no cross-table constraint exists).

    Returns:
        {status, part{...}}
  """
  actor = (actor or _current_actor())[:50]
  sku = (payload.get('sku') or '').strip().upper()
  part_desc = (payload.get('part_desc') or '').strip()

  if not sku or len(sku) > 50:
    return {'status': 'error', 'message': 'SKU is required (max 50 chars)'}
  if not part_desc:
    return {'status': 'error', 'message': 'Description is required'}

  conn = get_connection()
  try:
    cursor = conn.cursor()

    # Uniqueness across BOTH tables: the view hides pc rows shadowed by
    # part_specs, so check pc_part_master directly as well.
    cursor.execute("""
        SELECT
          (SELECT COUNT(*) FROM dbo.vw_part_master  WHERE sku = %s) +
          (SELECT COUNT(*) FROM dbo.pc_part_master WHERE sku = %s) AS hits
    """, (sku, sku))
    if cursor.fetchone()['hits'] > 0:
      return {'status': 'error',
              'message': f'SKU {sku} already exists (e21 or PC-created)'}

    cursor.execute("""
        INSERT INTO dbo.pc_part_master
            (sku, part_desc, line, series, model, body_mat, asme_class,
             end_connect, size, base_sku, attrib_value, special_notes,
             created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (sku, part_desc[:150],
          (payload.get('line') or None),
          (payload.get('series') or None),
          (payload.get('model') or None),
          (payload.get('body_mat') or None),
          (payload.get('asme_class') or None),
          (payload.get('end_connect') or None),
          (payload.get('size') or None),
          (payload.get('base_sku') or None),
          (_norm_attrib(payload.get('attrib_value'))[:150] or None),
          ((payload.get('special_notes') or '')[:500] or None),
          actor))
    conn.commit()

    cursor.execute("""
        SELECT sku, part_desc, line, series, model, body_mat, asme_class,
               end_connect, size, base_sku, attrib_value, sku_source
        FROM dbo.vw_part_master WHERE sku = %s
    """, (sku,))
    return {'status': 'success', 'part': serialize_row(cursor.fetchone())}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error creating SKU: {e}'}
  finally:
    conn.close()


# =============================================================================
# SALES ORDER NOTES (View Notes popup)
# =============================================================================

@anvil.server.callable
@perf_probe.timed
def get_so_all_notes(sales_order_number, sales_order_rel):
  """
    Every note on a sales order: header notes (nx_so_notes) once, then
    per-line notes. Source: dbo.sales_orders_due.

    Returns:
        {status, header_notes, lines: [{line_no, sku, sku_desc, qty,
         attrib_val, notes}]}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT nx_item_no, nx_sku, nx_sku_desc, nx_qty_ordered,
               nx_attrib_val, nx_notes, nx_so_notes
        FROM dbo.sales_orders_due
        WHERE nx_order_numb = %s AND nx_rel_numb = %s
        ORDER BY TRY_CAST(nx_item_no AS INT), nx_item_no
    """, (sales_order_number, sales_order_rel))
    rows = cursor.fetchall()
    if not rows:
      return {'status': 'success', 'header_notes': '',
              'lines': [],
              'message': ('No lines found on sales_orders_due for this SO '
                          '(order may be fully shipped/closed).')}

    header = ''
    for r in rows:
      if r['nx_so_notes']:
        header = _clean_notes(r['nx_so_notes'])
        break

    lines = [{'line_no': r['nx_item_no'],
              'sku': r['nx_sku'],
              'sku_desc': r['nx_sku_desc'],
              'qty': make_serializable(r['nx_qty_ordered']),
              'attrib_val': (r['nx_attrib_val'] or ''),
              'notes': _clean_notes(r['nx_notes'])}
             for r in rows]
    return {'status': 'success', 'header_notes': header, 'lines': lines}
  finally:
    conn.close()


# =============================================================================
# PART MASTER MANAGER (list / edit / retire) - SKU codes are IMMUTABLE
# =============================================================================

# Fields the manager may edit. NOT sku (immutable by policy - templates key
# on it), NOT exported_to_e21/exported_at (the export project owns those).
EDITABLE_FIELDS = ('part_desc', 'line', 'series', 'model', 'body_mat',
                   'asme_class', 'end_connect', 'size', 'base_sku',
                   'attrib_value', 'special_notes')


@anvil.server.callable
@perf_probe.timed
def list_pc_skus(search=None, include_inactive=False, unexported_only=False):
  """
    PC-created SKUs for the Part Master Manager grid (TOP 200, newest
    first). unexported_only=True is the future e21 export batch view.
  """
  where = ['1=1']
  params = []
  if search and search.strip():
    like = f'%{search.strip()}%'
    where.append('(sku LIKE %s OR part_desc LIKE %s OR base_sku LIKE %s '
                 'OR attrib_value LIKE %s)')
    params += [like, like, like, like]
  if not include_inactive:
    where.append('is_active = 1')
  if unexported_only:
    where.append('exported_to_e21 = 0')
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT TOP 200 sku, part_desc, series, body_mat, size, base_sku,
               attrib_value, special_notes, is_active,
               exported_to_e21, exported_at,
               created_at, created_by, updated_at, updated_by
        FROM dbo.pc_part_master
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC
    """, tuple(params))
    return [serialize_row(r) for r in cursor.fetchall()]
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def update_pc_sku(sku, fields, actor=None):
  """
    Edit a PC-created SKU's descriptive fields (whitelist above).
    attrib_value is normalized on save so matching stays canonical.
  """
  actor = (actor or _current_actor())[:50]
  sets, params = [], []
  for k in EDITABLE_FIELDS:
    if k in fields:
      v = fields[k]
      if k == 'attrib_value':
        v = _norm_attrib(v)[:150] or None
      elif k == 'special_notes':
        v = ((v or '')[:500] or None)
      else:
        v = ((v or '').strip()[:150] or None)
      sets.append(f'{k} = %s')
      params.append(v)
  if not sets:
    return {'status': 'error', 'message': 'Nothing to update'}
  sets.append('updated_at = GETDATE()')
  sets.append('updated_by = %s')
  params += [actor, sku]
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute(f"""
        UPDATE dbo.pc_part_master SET {', '.join(sets)} WHERE sku = %s
    """, tuple(params))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': f'PC SKU {sku} not found'}
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error updating SKU: {e}'}
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def set_pc_sku_active(sku, active, actor=None):
  """
    Soft retire / reinstate a PC SKU. Inactive SKUs drop out of
    vw_part_master (search, routing lookups, derived-special matching)
    but their rows, lineage and templates remain for history.
  """
  actor = (actor or _current_actor())[:50]
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE dbo.pc_part_master
        SET is_active = %s, updated_at = GETDATE(), updated_by = %s
        WHERE sku = %s
    """, (1 if active else 0, actor, sku))
    if cursor.rowcount == 0:
      return {'status': 'error', 'message': f'PC SKU {sku} not found'}
    conn.commit()
    return {'status': 'success'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error: {e}'}
  finally:
    conn.close()