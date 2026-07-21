"""
Anvil Server Module - Routing Dashboard
========================================
Server-side code for Work Order Routing v1 (Production Control app).

Implements the locked Routing v1 design (see documents/
Routing_Form_Specification.docx and Routing_Methodology_Proposal.docx):
    - Template/instance split: pc_routing_templates hold per-SKU defaults;
      each released WO gets its own editable snapshot in pc_wo_routing_steps.
    - Capture-on-first-release: no template -> PC builds the routing in the
      editor; save_wo_routing reports template_differs so the form can offer
      to save it as the SKU default.
    - Qty-per-step: Complete events carry qty; qty_completed accumulates.
    - Record-and-flag: out-of-order actions are stamped out_of_sequence = 1,
      never blocked.
    - Rework: reset_steps_for_rework() zeroes chosen steps via Reset events;
      prior Complete events remain (that work physically happened).

Tables (migration 002, applied 2026-07-14):
    dbo.pc_departments, dbo.pc_routing_templates,
    dbo.pc_routing_template_steps, dbo.pc_wo_routing_steps,
    dbo.pc_wo_routing_step_events

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


# Step statuses (CK_pc_wo_routing_steps_status)
STATUS_PENDING = 'Pending'
STATUS_READY = 'Ready'
STATUS_IN_PROGRESS = 'In Progress'
STATUS_COMPLETE = 'Complete'
STATUS_SKIPPED = 'Skipped'

# Event types (CK_pc_wo_step_events_type)
VALID_EVENT_TYPES = ('Ready', 'Start', 'Complete', 'Skip', 'Reset', 'Edit')


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


# =============================================================================
# DATABASE CONNECTION / ACTOR
# =============================================================================

def get_connection():
  """Create and return a database connection (as_dict=True cursors)."""
  return db_config.get_nexus_connection()


def _current_actor():
  """Best-effort current user for audit rows. Returns a display name/email
  when the Anvil Users service is enabled, else a clear fallback."""
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


def _actor_or_default(actor):
  """Normalize the actor argument: explicit value wins, else session user."""
  return (actor or _current_actor())[:50]


# =============================================================================
# DEPARTMENTS
# =============================================================================

@anvil.server.callable
def get_departments():
  """
    Active departments for the routing editor dropdown.

    Returns:
        list of {department_id, department_name} ordered by sort_order
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT department_id, department_name
        FROM dbo.pc_departments
        WHERE is_active = 1
        ORDER BY sort_order, department_name
    """)
    return [serialize_row(r) for r in cursor.fetchall()]
  finally:
    conn.close()


# =============================================================================
# GET ROUTING FOR RELEASE (editor pre-load)
# =============================================================================

@anvil.server.callable
def get_routing_for_release(work_order_id):
  """
    Everything the routing editor needs when a Release is initiated.

    Pre-load priority:
      1. Existing WO routing rows (source='wo')  - WO was released before
         (UnRelease / Reopen); the snapshot is never discarded.
      2. Active default template for the part (source='template').
      3. Nothing (source='none') - capture-on-first-release moment.

    Returns:
        {status, wo{...}, steps[{sequence_number, department_id,
         department_name, step_notes}], source, template_id}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT work_order_id, work_order_number, part_number,
               quantity, wo_override_qty, notes, so_notes,
               wo_due_date, due_date, status
        FROM dbo.work_orders
        WHERE work_order_id = %s
    """, (work_order_id,))
    wo = cursor.fetchone()
    if not wo:
      return {'status': 'error', 'message': f'Work order {work_order_id} not found'}

    wo['effective_qty'] = wo['wo_override_qty'] or wo['quantity']

    # Derived specials (spec v2 §2.5): PC-created SKUs based on this part.
    # WO part_number is NEVER repointed - these only offer a routing to load.
    cursor.execute("""
        SELECT m.sku, m.part_desc, m.special_notes,
               CASE WHEN EXISTS (SELECT 1 FROM dbo.pc_routing_templates t
                                 WHERE t.part_number = m.sku
                                   AND t.attribute_value IS NULL
                                   AND t.is_active = 1)
                    THEN 1 ELSE 0 END AS has_template
        FROM dbo.pc_part_master m
        WHERE m.base_sku = %s
        ORDER BY m.created_at
    """, (wo['part_number'],))
    derived = [serialize_row(r) for r in cursor.fetchall()]

    # 1. Existing WO routing?
    cursor.execute("""
        SELECT s.wo_step_id, s.sequence_number, s.department_id,
               d.department_name, s.step_notes, s.status,
               s.qty_required, s.qty_completed
        FROM dbo.pc_wo_routing_steps s
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE s.work_order_id = %s
        ORDER BY s.sequence_number, s.wo_step_id
    """, (work_order_id,))
    wo_steps = cursor.fetchall()
    if wo_steps:
      return {
        'status': 'success',
        'source': 'wo',
        'template_id': None,
        'wo': serialize_row(wo),
        'derived_specials': derived,
        'steps': [serialize_row(r) for r in wo_steps],
      }

    # 2. Active default template for this part (attribute_value NULL)?
    cursor.execute("""
        SELECT t.template_id, s.sequence_number, s.department_id,
               d.department_name, s.step_notes
        FROM dbo.pc_routing_templates t
        JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE t.part_number = %s
          AND t.attribute_value IS NULL
          AND t.is_active = 1
        ORDER BY s.sequence_number, s.template_step_id
    """, (wo['part_number'],))
    tpl_steps = cursor.fetchall()
    if tpl_steps:
      return {
        'status': 'success',
        'source': 'template',
        'template_id': tpl_steps[0]['template_id'],
        'wo': serialize_row(wo),
        'derived_specials': derived,
        'steps': [serialize_row(r) for r in tpl_steps],
      }

    # 3. Capture-on-first-release
    return {
      'status': 'success',
      'source': 'none',
      'template_id': None,
      'wo': serialize_row(wo),
      'derived_specials': derived,
      'steps': [],
    }
  finally:
    conn.close()


# =============================================================================
# GET SO CREATED WORK ORDERS (packet-release routing loop)
# =============================================================================

@anvil.server.callable
def get_so_created_work_orders(sales_order_number, sales_order_rel):
  """
    'Created' work orders on a sales order, for the packet-release routing
    loop in WorkOrderDetail (each WO gets a routing before its release;
    same-part WOs can reuse the first routing).

    Returns:
        {status, work_orders: [{work_order_id, work_order_number,
                                part_number}]}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT work_order_id, work_order_number, part_number
        FROM dbo.work_orders
        WHERE sales_order_number = %s
          AND sales_order_rel = %s
          AND status = 'Created'
        ORDER BY work_order_number
    """, (sales_order_number, sales_order_rel))
    return {'status': 'success',
            'work_orders': [serialize_row(r) for r in cursor.fetchall()]}
  finally:
    conn.close()


# =============================================================================
# SAVE WO ROUTING (Release-time snapshot)
# =============================================================================

@anvil.server.callable
def save_wo_routing(work_order_id, steps, actor=None):
  """
    Persist the routing snapshot for a work order (called by the editor's
    Save, BEFORE the form flips status to 'Released').

    steps: list of {department_id, step_notes} in display order. Sequence
    numbers are re-gapped 10/20/30 here; the client's order is authoritative.

    Guard: rejected when any existing step is In Progress or Complete -
    released-and-worked routings change via record/reset, never wholesale
    replace. Pre-release re-saves (UnRelease -> Release) replace the old
    snapshot, including its Ready/Edit events.

    Returns:
        {status, step_count, template_differs, part_number}
  """
  actor = _actor_or_default(actor)

  if not steps:
    return {'status': 'error', 'message': 'Routing must contain at least one step'}
  for s in steps:
    if not s.get('department_id'):
      return {'status': 'error', 'message': 'Every step needs a department'}

  conn = get_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT work_order_id, part_number, quantity, wo_override_qty
        FROM dbo.work_orders WHERE work_order_id = %s
    """, (work_order_id,))
    wo = cursor.fetchone()
    if not wo:
      return {'status': 'error', 'message': f'Work order {work_order_id} not found'}
    effective_qty = wo['wo_override_qty'] or wo['quantity']

    # Guard: no wholesale replace once work has been recorded.
    cursor.execute("""
        SELECT COUNT(*) AS worked
        FROM dbo.pc_wo_routing_steps
        WHERE work_order_id = %s
          AND (status IN (%s, %s) OR qty_completed > 0)
    """, (work_order_id, STATUS_IN_PROGRESS, STATUS_COMPLETE))
    if cursor.fetchone()['worked'] > 0:
      return {'status': 'error',
              'message': ('This routing has recorded work. Use Record Progress '
                          'or Reset for Rework instead of re-saving it.')}

    # Replace prior (unworked) snapshot: events first (FK), then steps.
    cursor.execute("""
        DELETE FROM dbo.pc_wo_routing_step_events WHERE work_order_id = %s
    """, (work_order_id,))
    cursor.execute("""
        DELETE FROM dbo.pc_wo_routing_steps WHERE work_order_id = %s
    """, (work_order_id,))

    # Re-gap sequence groups (spec v2): steps sharing a seq_group share a
    # sequence_number (PARALLEL). Falls back to list order when the client
    # sends no seq_group (v1 dialog compatibility). Every step in the FIRST
    # group starts Ready - parallel first steps all go live at release.
    groups = [s.get('seq_group') for s in steps]
    if any(g is None for g in groups):
      groups = list(range(1, len(steps) + 1))
    group_map = {g: (i + 1) * 10 for i, g in enumerate(sorted(set(groups)))}
    first_seq = 10
    for s, g in zip(steps, groups):
      seq = group_map[g]
      status = STATUS_READY if seq == first_seq else STATUS_PENDING
      cursor.execute("""
          INSERT INTO dbo.pc_wo_routing_steps
              (work_order_id, sequence_number, department_id, status,
               qty_required, step_notes, created_by)
          OUTPUT INSERTED.wo_step_id
          VALUES (%s, %s, %s, %s, %s, %s, %s)
      """, (work_order_id, seq, s['department_id'], status,
            effective_qty, (s.get('step_notes') or None), actor))
      wo_step_id = cursor.fetchone()['wo_step_id']

      cursor.execute("""
          INSERT INTO dbo.pc_wo_routing_step_events
              (wo_step_id, work_order_id, event_type, event_by, notes)
          VALUES (%s, %s, 'Edit', %s, 'release-save')
      """, (wo_step_id, work_order_id, actor))
      if seq == first_seq:
        cursor.execute("""
            INSERT INTO dbo.pc_wo_routing_step_events
                (wo_step_id, work_order_id, event_type, event_by, notes)
            VALUES (%s, %s, 'Ready', %s, 'first step at release')
        """, (wo_step_id, work_order_id, actor))

    # Does the saved routing differ from the active default template?
    cursor.execute("""
        SELECT s.sequence_number, s.department_id
        FROM dbo.pc_routing_templates t
        JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
        WHERE t.part_number = %s AND t.attribute_value IS NULL AND t.is_active = 1
        ORDER BY s.sequence_number, s.template_step_id
    """, (wo['part_number'],))
    tpl_pairs = sorted((r['sequence_number'], r['department_id'])
                       for r in cursor.fetchall())
    saved_pairs = sorted(zip((group_map[g] for g in groups),
                             (s['department_id'] for s in steps)))
    template_differs = (tpl_pairs != saved_pairs)

    conn.commit()
    return {'status': 'success',
            'step_count': len(steps),
            'template_differs': template_differs,
            'part_number': wo['part_number']}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error saving routing: {e}'}
  finally:
    conn.close()


# =============================================================================
# SAVE ROUTING TEMPLATE (the SKU default)
# =============================================================================

@anvil.server.callable
def save_routing_template(part_number, steps, actor=None):
  """
    Upsert the default (NULL-attribute) routing template for a part.
    Called when PC confirms the 'save as new default?' prompt.
    Never touches pc_wo_routing_steps.

    Returns:
        {status, template_id, created (bool)}
  """
  actor = _actor_or_default(actor)
  if not steps:
    return {'status': 'error', 'message': 'Template must contain at least one step'}

  conn = get_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT template_id FROM dbo.pc_routing_templates
        WHERE part_number = %s AND attribute_value IS NULL
    """, (part_number,))
    row = cursor.fetchone()

    if row:
      template_id = row['template_id']
      created = False
      cursor.execute("""
          DELETE FROM dbo.pc_routing_template_steps WHERE template_id = %s
      """, (template_id,))
      cursor.execute("""
          UPDATE dbo.pc_routing_templates
          SET is_active = 1, updated_at = GETDATE(), updated_by = %s
          WHERE template_id = %s
      """, (actor, template_id))
    else:
      created = True
      cursor.execute("""
          INSERT INTO dbo.pc_routing_templates (part_number, created_by)
          OUTPUT INSERTED.template_id
          VALUES (%s, %s)
      """, (part_number, actor))
      template_id = cursor.fetchone()['template_id']

    # Same group mapping as save_wo_routing: shared seq_group = parallel.
    groups = [s.get('seq_group') for s in steps]
    if any(g is None for g in groups):
      groups = list(range(1, len(steps) + 1))
    group_map = {g: (i + 1) * 10 for i, g in enumerate(sorted(set(groups)))}
    for s, g in zip(steps, groups):
      cursor.execute("""
          INSERT INTO dbo.pc_routing_template_steps
              (template_id, sequence_number, department_id, step_notes)
          VALUES (%s, %s, %s, %s)
      """, (template_id, group_map[g], s['department_id'],
            (s.get('step_notes') or None)))

    conn.commit()
    return {'status': 'success', 'template_id': template_id, 'created': created}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error saving template: {e}'}
  finally:
    conn.close()


# =============================================================================
# GET WO ROUTING (status panel)
# =============================================================================

@anvil.server.callable
def get_wo_routing(work_order_id):
  """
    Routing steps + progress for the read-only status panel.

    Returns:
        {status, steps[{... , has_oos, last_event_at, last_event_by}],
         oos_count, all_complete}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.wo_step_id, s.sequence_number, s.department_id,
               d.department_name, s.status, s.qty_required, s.qty_completed,
               s.step_notes,
               (SELECT COUNT(*) FROM dbo.pc_wo_routing_step_events e
                WHERE e.wo_step_id = s.wo_step_id
                  AND e.out_of_sequence = 1)          AS oos_events,
               (SELECT MAX(e.event_at) FROM dbo.pc_wo_routing_step_events e
                WHERE e.wo_step_id = s.wo_step_id)    AS last_event_at
        FROM dbo.pc_wo_routing_steps s
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE s.work_order_id = %s
        ORDER BY s.sequence_number, s.wo_step_id
    """, (work_order_id,))
    rows = cursor.fetchall()

    steps = []
    oos_count = 0
    active = [r for r in rows if r['status'] != STATUS_SKIPPED]
    for r in rows:
      r['has_oos'] = r['oos_events'] > 0
      oos_count += r['oos_events']
      steps.append(serialize_row(r))

    all_complete = bool(active) and all(
      r['status'] == STATUS_COMPLETE for r in active)

    return {'status': 'success', 'steps': steps,
            'oos_count': oos_count, 'all_complete': all_complete}
  finally:
    conn.close()


# =============================================================================
# RECORD STEP EVENT (progress from the status panel)
# =============================================================================

@anvil.server.callable
def record_step_event(wo_step_id, event_type, qty=None, actor=None, notes=None):
  """
    Record progress on a routing step.

      Start    - work began (qty ignored). Status -> In Progress.
      Complete - qty pieces finished here (qty required, > 0).
                 qty_completed += qty; status -> In Progress, or Complete
                 when qty_completed >= qty_required.
      Skip     - PC skips the step. Status -> Skipped.

    Out-of-sequence (record-and-flag, never blocked): the event is stamped
    out_of_sequence = 1 when any earlier-sequence step on the WO has
    qty_completed < this step's new cumulative total (for Start/Skip:
    when any earlier step is not Complete/Skipped).

    Ready cascade: after any change, every Pending step whose earlier
    steps ALL have qty_completed > 0 (or are Skipped) becomes Ready.

    Returns:
        {status, step{...}, oos (bool), wo_complete (bool)}
  """
  actor = _actor_or_default(actor)

  if event_type not in ('Start', 'Complete', 'Skip'):
    return {'status': 'error',
            'message': f"record_step_event handles Start/Complete/Skip, not '{event_type}'"}
  if event_type == 'Complete':
    if qty is None or int(qty) <= 0:
      return {'status': 'error', 'message': 'Complete requires a qty greater than 0'}
    qty = int(qty)
  else:
    qty = None

  conn = get_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT wo_step_id, work_order_id, sequence_number, status,
               qty_required, qty_completed
        FROM dbo.pc_wo_routing_steps WHERE wo_step_id = %s
    """, (wo_step_id,))
    step = cursor.fetchone()
    if not step:
      return {'status': 'error', 'message': f'Step {wo_step_id} not found'}
    if step['status'] == STATUS_SKIPPED:
      return {'status': 'error', 'message': 'Step is Skipped - reset it first'}

    work_order_id = step['work_order_id']

    # All sibling steps (for OOS check + cascade)
    cursor.execute("""
        SELECT wo_step_id, sequence_number, status, qty_completed
        FROM dbo.pc_wo_routing_steps
        WHERE work_order_id = %s
        ORDER BY sequence_number, wo_step_id
    """, (work_order_id,))
    siblings = cursor.fetchall()
    earlier = [s for s in siblings
               if s['sequence_number'] < step['sequence_number']]

    # --- OOS determination ---
    if event_type == 'Complete':
      new_total = step['qty_completed'] + qty
      oos = any(s['status'] != STATUS_SKIPPED and s['qty_completed'] < new_total
                for s in earlier)
    else:
      oos = any(s['status'] not in (STATUS_COMPLETE, STATUS_SKIPPED)
                for s in earlier)

    # --- Apply the event ---
    if event_type == 'Complete':
      new_total = step['qty_completed'] + qty
      new_status = (STATUS_COMPLETE if new_total >= step['qty_required']
                    else STATUS_IN_PROGRESS)
      cursor.execute("""
          UPDATE dbo.pc_wo_routing_steps
          SET qty_completed = %s, status = %s WHERE wo_step_id = %s
      """, (new_total, new_status, wo_step_id))
    elif event_type == 'Start':
      new_status = STATUS_IN_PROGRESS
      cursor.execute("""
          UPDATE dbo.pc_wo_routing_steps
          SET status = %s WHERE wo_step_id = %s
      """, (new_status, wo_step_id))
    else:  # Skip
      new_status = STATUS_SKIPPED
      cursor.execute("""
          UPDATE dbo.pc_wo_routing_steps
          SET status = %s WHERE wo_step_id = %s
      """, (new_status, wo_step_id))

    cursor.execute("""
        INSERT INTO dbo.pc_wo_routing_step_events
            (wo_step_id, work_order_id, event_type, qty,
             out_of_sequence, event_by, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (wo_step_id, work_order_id, event_type, qty,
          1 if oos else 0, actor, (notes or None)))

    # --- Ready cascade (re-read siblings with the update applied) ---
    cursor.execute("""
        SELECT wo_step_id, sequence_number, status, qty_completed
        FROM dbo.pc_wo_routing_steps
        WHERE work_order_id = %s
        ORDER BY sequence_number, wo_step_id
    """, (work_order_id,))
    siblings = cursor.fetchall()
    for s in siblings:
      if s['status'] != STATUS_PENDING:
        continue
      preds = [p for p in siblings
               if p['sequence_number'] < s['sequence_number']]
      if all(p['status'] == STATUS_SKIPPED or p['qty_completed'] > 0
             for p in preds):
        cursor.execute("""
            UPDATE dbo.pc_wo_routing_steps
            SET status = %s WHERE wo_step_id = %s
        """, (STATUS_READY, s['wo_step_id']))
        cursor.execute("""
            INSERT INTO dbo.pc_wo_routing_step_events
                (wo_step_id, work_order_id, event_type, event_by, notes)
            VALUES (%s, %s, 'Ready', %s, 'auto: predecessor progress')
        """, (s['wo_step_id'], work_order_id, actor))

    # --- WO-complete signal ---
    cursor.execute("""
        SELECT COUNT(*) AS open_steps
        FROM dbo.pc_wo_routing_steps
        WHERE work_order_id = %s AND status NOT IN (%s, %s)
    """, (work_order_id, STATUS_COMPLETE, STATUS_SKIPPED))
    wo_complete = (cursor.fetchone()['open_steps'] == 0)

    conn.commit()

    cursor.execute("""
        SELECT s.wo_step_id, s.sequence_number, s.department_id,
               d.department_name, s.status, s.qty_required, s.qty_completed
        FROM dbo.pc_wo_routing_steps s
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE s.wo_step_id = %s
    """, (wo_step_id,))
    return {'status': 'success',
            'step': serialize_row(cursor.fetchone()),
            'oos': oos,
            'wo_complete': wo_complete}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error recording event: {e}'}
  finally:
    conn.close()


# =============================================================================
# RESET STEPS FOR REWORK (Reopen path)
# =============================================================================

@anvil.server.callable
def reset_steps_for_rework(work_order_id, from_sequence, actor=None):
  """
    Rework re-entry: PC picks the step where work resumes. Every
    non-Skipped step with sequence_number >= from_sequence is reset:
    qty_completed -> 0; the re-entry step goes Ready, later ones Pending.
    One Reset event per step. Prior Complete events are retained.

    Returns:
        {status, reset_count}
  """
  actor = _actor_or_default(actor)

  conn = get_connection()
  try:
    cursor = conn.cursor()

    cursor.execute("""
        SELECT wo_step_id, sequence_number, status
        FROM dbo.pc_wo_routing_steps
          AND status <> %s
        ORDER BY sequence_number, wo_step_id
    """, (work_order_id, from_sequence, STATUS_SKIPPED))
    targets = cursor.fetchall()
    if not targets:
      return {'status': 'error',
              'message': f'No steps at sequence {from_sequence} or later to reset'}

    reentry_seq = targets[0]['sequence_number']
    for t in targets:
      new_status = STATUS_READY if t['sequence_number'] == reentry_seq else STATUS_PENDING
      cursor.execute("""
          UPDATE dbo.pc_wo_routing_steps
          SET qty_completed = 0, status = %s WHERE wo_step_id = %s
      """, (new_status, t['wo_step_id']))
      cursor.execute("""
          INSERT INTO dbo.pc_wo_routing_step_events
              (wo_step_id, work_order_id, event_type, event_by, notes)
          VALUES (%s, %s, 'Reset', %s, %s)
      """, (t['wo_step_id'], work_order_id, actor,
            f'rework re-entry at sequence {reentry_seq}'))

    conn.commit()
    return {'status': 'success', 'reset_count': len(targets)}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error resetting steps: {e}'}
  finally:
    conn.close()


# =============================================================================
# GET TEMPLATE FOR SKU (Routing Library mode + derived-specials load)
# =============================================================================

@anvil.server.callable
def get_template_for_sku(sku):
  """
    Default routing template for any SKU (LIBRARY mode browsing, and
    loading a derived special's routing from the §2.5 suggestion banner).

    Returns:
        {status, template_id (or None), steps[{sequence_number,
         department_id, department_name, step_notes}]}
  """
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.template_id, s.sequence_number, s.department_id,
               d.department_name, s.step_notes
        FROM dbo.pc_routing_templates t
        JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE t.part_number = %s AND t.attribute_value IS NULL
          AND t.is_active = 1
        ORDER BY s.sequence_number, s.template_step_id
    """, (sku,))
    rows = cursor.fetchall()
    return {'status': 'success',
            'template_id': rows[0]['template_id'] if rows else None,
            'steps': [serialize_row(r) for r in rows]}
  finally:
    conn.close()
