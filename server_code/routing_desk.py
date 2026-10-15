"""
Anvil Server Module - Routing Desk
==================================
Server side of the Routing Desk form (Production Control v2.0).

The Desk is one screen: a bounded queue of work orders that need routing
(today's published plan by default) beside a pane that edits the selected
order's routing. This module gives the form everything it shows in ONE call,
saves routings WITHOUT releasing, and owns the release operation that used to
be split between the editor and a bare status flip.

Design (documents/Production_Control_v2_Build_Readiness_Review_2026-10-05.md,
P1, P5, P6):

    * Plan-first. open_routing_desk('plan') returns every work order on
      TODAY'S plan (Created + Released) in plan order; 'created' returns the
      Created backlog capped at MAX_ROWS, plan rows first, then by due date.
    * One call to open, zero to browse, one to save (or one per batch).
      Rows are plain list[dict]; steps for routed rows come back in the same
      payload (one IN query), as do the active default templates for every
      loaded part and the PC specials derived from them (route source).
    * save_routing writes every step PENDING. Nothing goes Ready and no
      department is texted until release_work_order runs. A routing saved
      but not released is therefore invisible to every department board
      (vw_pc_department_queue consumers filter on step_status) - which is
      what "Routing saved; order not released" has to mean.
    * release_work_order is the ONLY path to work_orders.status = 'Released':
      guard (Created, routing exists, nothing worked), first group -> Ready,
      Ready events, department text, status flip, action-log row, one
      transaction. work_order_dashboard.update_work_order_status delegates
      'Released' here so the old editor path cannot bypass the guard.
    * Expected-version check (detect-and-refuse): the client holds the
      routing's version token (MAX(created_at) of its step rows, or None)
      and the save refuses with the other planner's steps when it moved.
      No new table, no migration (review P5).
    * Lineage: pc_wo_routing_steps.source_template_id (column since 002,
      never written before this module) records which template the copy came
      from. Edit events are noted 'desk-save' so the data tells the Desk's
      writes apart from the old editor ('release-save') and the external
      batch writer ('batch release-save').
    * Department texts (review D8, 2026-10-06): a Ready step whose
      ROUTING_READY_<dept_code> token has no active subscriber is written to
      pc_notifications as Failed / attempts 3 / "no active recipients" - one
      audit row, no three retries, no Failed pile-up in the drain.
    * Default routing (migration 108, Dave's simplified_routing.pdf, decided
      2026-10-07 / scoped 2026-10-13): every work order is routed at CREATION
      by dbo.sp_pc_apply_default_routing - the part's active template when
      it is a real route, else the wo_type default (*STANDARD F>P>A,
      *NON-STANDARD F>P>B>A) - as Pending steps. So the Desk is
      exception-only: the payload carries the two defaults, every row says
      what its expected route is and whether it is an exception, and release
      is a one-click status flip. Material Handling (F) and Pre-Assembly (P)
      have no board: Production Control completes those steps from the Desk
      (complete_desk_step -> sp_pc_complete_step, the single write path),
      typing the element dimensions into the step notes on the way.

Tables: dbo.work_orders (status, routing lineage via steps),
dbo.pc_wo_routing_steps, dbo.pc_wo_routing_step_events, dbo.pc_notifications,
dbo.work_order_action_log, dbo.pc_routing_templates(+_steps),
dbo.pc_departments, dbo.pc_part_master / dbo.vw_part_master,
dbo.sales_orders_due, dbo.attrib_val_default, dbo.work_order_changes,
dbo.sms_notification_contacts (read: recipient check only).
Procs: dbo.sp_pc_complete_step (007/011, EXEC only).

No DDL. Database: SQL Server (nexus) via pymssql, credentials via db_config.

Author: Claude (Dave Whritenour review) | Project: Titan Flow Control - Year of Automation
Created: 2026-10-06
"""

# =============================================================================
# Version:       1.2.1
# Last updated:  2026-10-13
# Changelog (this module only):
#   1.2.1 (2026-10-15) Release resumes an un-released order: readies the first OPEN
#                      group instead of refusing a routing with recorded work
#                      (auto-completed F / P stay Complete). Action 'Release (resume)'.
#   1.2.0 (2026-10-14) Auto-complete at release for departments with no board
#                      yet (global_parameters pc_auto_complete_<code> = 1,
#                      migration 111; F and P today) - the Ready step completes
#                      through sp_pc_complete_step with actor AUTO, texts D8'd,
#                      so the order lands at SE / Assemble as before 108. Retire
#                      per department by setting the parameter to 0. Quick
#                      Release (v1.0 grid) calls release_work_order(s) with
#                      source 'quick'. _d8_after_proc shared by both paths.
#   1.1.1 (2026-10-14) 'wo_type' counts as a material change (migration 110 logs
#                      FIELD_CHANGE wo_type when a changed element flips the type).
#   1.1.0 (2026-10-13) Default routing (migration 108). Payload: `defaults`
#                      (the *STANDARD / *NON-STANDARD library routes), per
#                      row expected_route_summary / expected_source /
#                      is_exception. save_routing: is_exception and
#                      prompt_part_default (no "save as default?" nag for an
#                      A-only routing or one equal to the wo_type default);
#                      route comparison by dense-ranked groups, not raw
#                      sequence numbers. NEW complete_desk_step: PC completes
#                      a Ready F / P step of a Released order from the Desk
#                      via sp_pc_complete_step, writes the element dimensions
#                      into the P step and every later step's notes, and
#                      applies D8 to what the proc queued (PC's own
#                      step-complete text suppressed, recipient-less
#                      departments get an audit row, no retries).
#   1.0.0 (2026-10-06) NEW - Routing Desk slice 1. open_routing_desk /
#                      search_desk_orders / refresh_desk_rows (payload),
#                      save_routing (Pending-only, lineage, expected version),
#                      release_work_order + release_work_orders (guarded, one
#                      transaction per WO), save_and_release_work_order,
#                      demote_ready_steps (UnRelease helper used by
#                      work_order_dashboard and work_order_packet_pdf).
# =============================================================================

__version__       = "1.2.1"
__version_date__  = "2026-10-13"
__version_notes__ = ("Default routing (108) + parameter-switched auto-complete of boardless "
                     "departments at release (111); Quick Release source")

import anvil.server
from datetime import datetime, date
from decimal import Decimal

from . import db_config
from . import perf_probe                                  # timing probe (remove with perf_probe.py)
from . import sales_orders_due_dashboard as _sod          # rush predicate - single source
from .part_master import _clean_notes                     # e21 note cleanup - single source


# =============================================================================
# CONSTANTS
# =============================================================================

STATUS_PENDING     = 'Pending'
STATUS_READY       = 'Ready'
STATUS_IN_PROGRESS = 'In Progress'
STATUS_COMPLETE    = 'Complete'
STATUS_SKIPPED     = 'Skipped'

WO_CREATED  = 'Created'
WO_RELEASED = 'Released'

MAX_ROWS         = 300   # 'created' / 'routed' views: TOP (MAX_ROWS + 1) detects truncation
SEARCH_ROWS      = 100   # search_desk_orders cap
BATCH_MAX        = 40    # release_work_orders per call: ~8 statements x ~40 ms Anvil->Nexus
# per WO keeps one call well inside Anvil's 30 s limit (the
# Accept Orphans lesson, migration 084). Clients chunk above this.
INLINE_NOTES_MAX = 60    # full (cleaned) notes ride inline when the row set is this small;
# larger sets carry has_notes + preview and the client calls
# get_wo_notes on first selection (same as the grids since 09-24)

# Mirrors routing_notifications.MAX_ATTEMPTS. Kept local on purpose: this
# module must not import the Twilio client just to know the retry ceiling.
NOTIFY_MAX_ATTEMPTS = 3

# Change types / fields that matter to routing (review F6 / M4): everything
# except the live-display fields the grid already shows via COALESCE.
MATERIAL_CHANGE_FIELDS = ('quantity', 'attribute_value', 'wo_type')   # wo_type: migration 110

# Migration 108: the wo_type default routes live in the template library
# under part numbers no SKU can collide with. wo_type NULL counts as Standard
# (same rule as sp_pc_apply_default_routing).
DEFAULT_TEMPLATE_PARTS = {'Standard': '*STANDARD', 'Non-Standard': '*NON-STANDARD'}

# Departments with no board of their own: Production Control completes their
# steps from the Desk (Released orders only). Everyone else completes from
# their own app - the Desk refuses other departments' steps on the server.
DESK_COMPLETABLE_DEPTS = ('F', 'P')     # F Material Handling, P Pre-Assembly
DIMS_TAG = 'Dims:'                      # marker for the element dimensions in step notes


# =============================================================================
# SERIALIZATION / ACTOR HELPERS
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
  """Nexus connection (as_dict=True cursors) via centralized config."""
  return db_config.get_nexus_connection()


def _current_actor():
  """Best-effort current user for audit rows: Anvil Users when enabled,
  else the module default (the Desk passes the picker's name as `actor`)."""
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
  """Explicit actor wins (the Desk's 'Desk: name' picker), else session user."""
  a = (actor or '').strip()
  return (a or _current_actor())[:50]


def _norm_attrib(v):
  """Canonical attribute form for matching: uppercase, whitespace collapsed."""
  return ' '.join((v or '').upper().split())


def _plan_label(tier, position, plan_date_iso, on_today):
  """'M-3' for today's plan; 'M-3 (09/08)' when the stamp is an older day
  (same rule as work_order_dashboard._plan_label)."""
  if not tier or position is None:
    return ''
  label = f"{str(tier)[0].upper()}-{int(position)}"
  if not on_today and plan_date_iso:
    try:
      label += f" ({datetime.fromisoformat(plan_date_iso).strftime('%m/%d')})"
    except (ValueError, TypeError):
      pass
  return label


def _route_summary(steps):
  """'A>E>H' from ordered step dicts; a parallel group joins with '+'
  ('A+B>E'). Steps must be sorted by sequence_number."""
  groups = {}
  order = []
  for s in steps:
    g = s.get('sequence_number')
    if g not in groups:
      groups[g] = []
      order.append(g)
    groups[g].append(s.get('dept_code') or '?')
  return '>'.join('+'.join(groups[g]) for g in order)


def _route_sig(steps):
  """Comparable shape of a route: sorted (group rank, department_id) pairs,
  where the rank is the dense rank of sequence_number. 10/20/30 from the
  library and 1/2/3 from an editor are the same route; parallel steps
  (shared sequence_number) share a rank. Works for WO step rows, template
  step rows and the editor's {seq_group, department_id} payload."""
  seqs = []
  for s in steps:
    g = s.get('sequence_number')
    if g is None:
      g = s.get('seq_group')
    seqs.append(g)
  if any(g is None for g in seqs):
    seqs = list(range(1, len(seqs) + 1))
  rank = {g: i + 1 for i, g in enumerate(sorted(set(seqs)))}
  return tuple(sorted((rank[g], int(s['department_id']))
                      for s, g in zip(steps, seqs) if s.get('department_id')))


def _wo_type_key(wo_type):
  return 'Non-Standard' if wo_type == 'Non-Standard' else 'Standard'


def _expected_route(part_number, wo_type, templates, defaults):
  """What sp_pc_apply_default_routing would write for this order: the
  part's active template if any, else the wo_type default. Returns
  (template dict or None, source) with source 'part' | 'Standard' |
  'Non-Standard' | None."""
  tpl = templates.get(part_number) if templates else None
  if tpl and tpl.get('steps'):
    return tpl, 'part'
  key = _wo_type_key(wo_type)
  d = (defaults or {}).get(key)
  if d and d.get('steps'):
    return d, key
  return None, None


def _version_token(cursor, work_order_id):
  """The routing's version: MAX(created_at) over its step rows as an ISO
  string, or None when the WO has no routing. Compared as strings."""
  cursor.execute("""
      SELECT MAX(created_at) AS v FROM dbo.pc_wo_routing_steps
      WHERE work_order_id = %s
  """, (work_order_id,))
  row = cursor.fetchone()
  v = row['v'] if row else None
  return make_serializable(v)


# =============================================================================
# DESK PAYLOAD
# =============================================================================

def _desk_rows(cursor, where_sql, params, top=None, inline_notes=False):
  """Queue rows for the Desk. One statement: work order + SO line context +
  standard config + part master + default template + routing summary +
  material-change count. Returns (rows, truncated)."""
  rush_case = _sod._rush_case_sql('so.')
  top_clause = f"TOP ({int(top) + 1}) " if top else ''
  if inline_notes:
    notes_cols = """
            CAST(COALESCE(so.nx_notes, wo.notes) AS VARCHAR(MAX))       AS line_notes_raw,
            CAST(COALESCE(so.nx_so_notes, wo.so_notes) AS VARCHAR(MAX)) AS so_notes_raw,"""
  else:
    notes_cols = """
            CAST(NULL AS VARCHAR(10)) AS line_notes_raw,
            CAST(NULL AS VARCHAR(10)) AS so_notes_raw,"""
  cursor.execute(f"""
        SELECT {top_clause}
            wo.work_order_id, wo.work_order_number, wo.status, wo.wo_type,
            wo.sales_order_number, wo.sales_order_rel, wo.sales_order_line,
            wo.part_number, wo.attribute_value, wo.quantity, wo.wo_override_qty,
            wo.due_date, wo.wo_due_date, wo.created_at,
            wo.plan_date, wo.plan_tier, wo.plan_position,
            CASE WHEN wo.plan_date = CAST(GETDATE() AS DATE) THEN 1 ELSE 0 END AS on_todays_plan,
            wo.pending_change_count,
            COALESCE(so.nx_sku_desc, wo.sku_desc)         AS sku_desc,
            COALESCE(so.nx_attrib_val, wo.attribute_value) AS so_attrib_val,
            ad.nx_attribute_value                          AS standard_attrib_val,
            CASE WHEN so.nx_part_status = 'N' THEN 1 ELSE 0 END AS is_nonstock,
            so.nx_carrier, so.nx_mos,
            {rush_case}                                    AS is_rush,
            CASE WHEN (so.nx_notes IS NOT NULL AND LTRIM(RTRIM(CAST(so.nx_notes AS varchar(200)))) <> '')
                   OR (wo.notes IS NOT NULL AND LTRIM(RTRIM(CAST(wo.notes AS varchar(200)))) <> '')
                 THEN 1 ELSE 0 END                         AS has_line_notes,
            CASE WHEN (so.nx_so_notes IS NOT NULL AND LTRIM(RTRIM(CAST(so.nx_so_notes AS varchar(200)))) <> '')
                   OR (wo.so_notes IS NOT NULL AND LTRIM(RTRIM(CAST(wo.so_notes AS varchar(200)))) <> '')
                 THEN 1 ELSE 0 END                         AS has_so_notes,
            LEFT(CAST(COALESCE(so.nx_notes, wo.notes, so.nx_so_notes, wo.so_notes) AS varchar(200)), 60) AS notes_preview,
            {notes_cols}
            pm.part_desc, pm.body_mat, pm.size, pm.series, pm.base_sku, pm.sku_source,
            tpl.template_id                                AS part_template_id,
            (SELECT COUNT(*) FROM dbo.work_order_changes c
              WHERE c.work_order_id = wo.work_order_id
                AND c.acknowledged_at IS NULL
                AND (c.change_type <> 'FIELD_CHANGE'
                     OR c.field_name IN ({", ".join(["%s"] * len(MATERIAL_CHANGE_FIELDS))})))
                                                           AS material_change_count,
            rs.step_count, rs.routing_version, rs.source_template_id, rs.saved_by
        FROM dbo.work_orders wo
        LEFT JOIN dbo.sales_orders_due so
               ON wo.sales_order_number = so.nx_order_numb
              AND CAST(wo.sales_order_line AS VARCHAR) = so.nx_item_no
              AND wo.sales_order_rel = so.nx_rel_numb
        LEFT JOIN dbo.attrib_val_default ad
               ON wo.part_number = ad.nx_part_code
        LEFT JOIN dbo.vw_part_master pm
               ON pm.sku = wo.part_number
        LEFT JOIN dbo.pc_routing_templates tpl
               ON tpl.part_number = wo.part_number
              AND tpl.attribute_value IS NULL AND tpl.is_active = 1
        OUTER APPLY (
            SELECT COUNT(*) AS step_count, MAX(s.created_at) AS routing_version,
                   MIN(s.source_template_id) AS source_template_id,
                   MIN(s.created_by) AS saved_by
            FROM dbo.pc_wo_routing_steps s WHERE s.work_order_id = wo.work_order_id
        ) rs
        WHERE {where_sql}
        ORDER BY on_todays_plan DESC,
                 ISNULL(wo.plan_position, 2147483647),
                 COALESCE(wo.wo_due_date, wo.due_date),
                 wo.work_order_number
  """, tuple(MATERIAL_CHANGE_FIELDS) + tuple(params))
  raw = cursor.fetchall()
  truncated = bool(top) and len(raw) > int(top)
  if truncated:
    raw = raw[:int(top)]

  rows = []
  for r in raw:
    d = serialize_row(r)
    attr_val = (d.get('attribute_value') or '').strip()
    parts = [p.strip() for p in attr_val.split('|')]
    d['material_value'] = parts[0] if parts else ''
    d['se_value'] = parts[1] if len(parts) > 1 else ''
    d['effective_qty'] = d.get('wo_override_qty') or d.get('quantity')
    rel = d.get('sales_order_rel') or '001'
    d['so_number_display'] = f"{d.get('sales_order_number', '')}-{rel}"
    d['on_todays_plan'] = int(d.get('on_todays_plan') or 0)
    d['plan_label'] = _plan_label(d.get('plan_tier'), d.get('plan_position'),
                                  d.get('plan_date'), d['on_todays_plan'])
    d['is_rush'] = bool(d.get('is_rush'))
    d['is_nonstock'] = int(d.get('is_nonstock') or 0)
    d['is_nonstandard'] = 1 if (d.get('wo_type') == 'Non-Standard') else 0
    d['has_line_notes'] = int(d.get('has_line_notes') or 0)
    d['has_so_notes'] = int(d.get('has_so_notes') or 0)
    d['has_notes'] = 1 if (d['has_line_notes'] or d['has_so_notes']) else 0
    d['notes_preview'] = (d.get('notes_preview') or '').strip()
    d['sku_desc'] = (d.get('sku_desc') or '').strip()
    d['nx_carrier'] = (d.get('nx_carrier') or '').strip()
    d['nx_mos'] = (d.get('nx_mos') or '').strip()
    d['material_change_count'] = int(d.get('material_change_count') or 0)
    d['step_count'] = int(d.get('step_count') or 0)
    d['has_template'] = 1 if d.get('part_template_id') else 0
    # Ordered vs standard element configuration (same rule as WorkOrderDetail)
    std = _norm_attrib(d.get('standard_attrib_val'))
    ordered = _norm_attrib(d.get('so_attrib_val'))
    if not std or not ordered:
      d['config_match'] = 'unknown'
    elif std == ordered:
      d['config_match'] = 'matches'
    else:
      d['config_match'] = 'differs'
    # Routing state for the queue chip. Anything past Created (Released,
    # Complete, the -Verify statuses reached by a merge or a search) is
    # read-only on the Desk and sits under the Released chip.
    if d.get('status') != WO_CREATED:
      d['routing_state'] = 'released'
    elif d['step_count'] > 0:
      d['routing_state'] = 'routed'
    else:
      d['routing_state'] = 'none'
    # Notes: inline + cleaned when the set is small, else preview only
    if inline_notes:
      d['line_notes'] = _clean_notes(d.get('line_notes_raw'))
      d['so_notes'] = _clean_notes(d.get('so_notes_raw'))
      d['notes_inline'] = 1
    else:
      d['line_notes'] = None
      d['so_notes'] = None
      d['notes_inline'] = 0
    d.pop('line_notes_raw', None)
    d.pop('so_notes_raw', None)
    d['route_summary'] = ''          # filled from steps_by_wo below
    rows.append(d)
  return rows, truncated


def _steps_for(cursor, work_order_ids):
  """Routing steps for a set of WOs in ONE query -> {str(wo_id): [step, ...]}
  (string keys: the payload is JSON on the wire)."""
  ids = [int(i) for i in work_order_ids if i is not None]
  out = {}
  if not ids:
    return out
  ph = ", ".join(["%s"] * len(ids))
  cursor.execute(f"""
      SELECT s.wo_step_id, s.work_order_id, s.sequence_number, s.department_id,
             d.dept_code, d.department_name, s.status, s.step_notes,
             s.qty_required, s.qty_completed, s.source_template_id,
             s.created_at, s.created_by
      FROM dbo.pc_wo_routing_steps s
      JOIN dbo.pc_departments d ON d.department_id = s.department_id
      WHERE s.work_order_id IN ({ph})
      ORDER BY s.work_order_id, s.sequence_number, s.wo_step_id
  """, tuple(ids))
  for r in cursor.fetchall():
    out.setdefault(str(r['work_order_id']), []).append(serialize_row(r))
  return out


def _is_real_route(tpl):
  """A one-step Assemble template is not a route (108): it is the pre-108
  habit the wo_type default supersedes. Same rule as
  sp_pc_apply_default_routing, so the Desk's expectation never disagrees
  with what the proc writes - even if the v1.0 editor re-activates one."""
  steps = (tpl or {}).get('steps') or []
  return bool(steps) and not (len(steps) == 1 and (steps[0].get('dept_code') or '').upper() == 'A')


def _templates_for_parts(cursor, part_numbers):
  """Active default templates (attribute_value NULL) for the loaded parts,
  with steps -> {part_number: {template_id, steps[...]}}. One-step
  Assemble templates are dropped (_is_real_route)."""
  parts = sorted({p for p in part_numbers if p})
  out = {}
  if not parts:
    return out
  ph = ", ".join(["%s"] * len(parts))
  cursor.execute(f"""
      SELECT t.template_id, t.part_number, t.updated_at, t.updated_by,
             t.created_at, t.created_by,
             s.template_step_id, s.sequence_number, s.department_id,
             d.dept_code, d.department_name, s.step_notes
      FROM dbo.pc_routing_templates t
      JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
      JOIN dbo.pc_departments d ON d.department_id = s.department_id
      WHERE t.is_active = 1 AND t.attribute_value IS NULL
        AND t.part_number IN ({ph})
      ORDER BY t.part_number, s.sequence_number, s.template_step_id
  """, tuple(parts))
  for r in cursor.fetchall():
    r = serialize_row(r)
    entry = out.setdefault(r['part_number'], {
      'template_id': r['template_id'], 'part_number': r['part_number'],
      'saved_at': r.get('updated_at') or r.get('created_at'),
      'saved_by': r.get('updated_by') or r.get('created_by'),
      'steps': []})
    entry['steps'].append({k: r[k] for k in (
      'sequence_number', 'department_id', 'dept_code', 'department_name', 'step_notes')})
  out = {p: e for p, e in out.items() if _is_real_route(e)}
  for e in out.values():
    e['route_summary'] = _route_summary(e['steps'])
  return out


def _specials_for_parts(cursor, part_numbers):
  """PC-created specials derived from the loaded parts (pc_part_master.
  base_sku) that have an active default template - spec v2 Section 2.5.
  The client offers them as a route source when the WO's ORDERED
  configuration matches the special's stored attributes (attrib_norm)."""
  parts = sorted({p for p in part_numbers if p})
  if not parts:
    return []
  ph = ", ".join(["%s"] * len(parts))
  cursor.execute(f"""
      SELECT m.sku, m.base_sku, m.part_desc, m.attrib_value, m.special_notes,
             t.template_id,
             s.sequence_number, s.department_id, d.dept_code, d.department_name,
             s.step_notes, s.template_step_id
      FROM dbo.pc_part_master m
      JOIN dbo.pc_routing_templates t
        ON t.part_number = m.sku AND t.attribute_value IS NULL AND t.is_active = 1
      JOIN dbo.pc_routing_template_steps s ON s.template_id = t.template_id
      JOIN dbo.pc_departments d ON d.department_id = s.department_id
      WHERE m.is_active = 1 AND m.base_sku IN ({ph})
      ORDER BY m.base_sku, m.sku, s.sequence_number, s.template_step_id
  """, tuple(parts))
  by_sku = {}
  for r in cursor.fetchall():
    r = serialize_row(r)
    e = by_sku.setdefault(r['sku'], {
      'sku': r['sku'], 'base_sku': r['base_sku'], 'part_desc': r['part_desc'],
      'attrib_value': r['attrib_value'] or '',
      'attrib_norm': _norm_attrib(r['attrib_value']),
      'special_notes': r['special_notes'] or '',
      'template_id': r['template_id'], 'steps': []})
    e['steps'].append({k: r[k] for k in (
      'sequence_number', 'department_id', 'dept_code', 'department_name', 'step_notes')})
  for e in by_sku.values():
    e['route_summary'] = _route_summary(e['steps'])
  return list(by_sku.values())


def _departments(cursor):
  cursor.execute("""
      SELECT department_id, department_name, dept_code, sort_order
      FROM dbo.pc_departments
      WHERE is_active = 1
      ORDER BY sort_order, department_name
  """)
  return [serialize_row(r) for r in cursor.fetchall()]


def _totals(cursor):
  """Chip totals independent of the loaded view."""
  cursor.execute("""
      SELECT
        (SELECT COUNT(*) FROM dbo.work_orders
          WHERE plan_date = CAST(GETDATE() AS DATE) AND status <> 'Cancelled')   AS plan_total,
        (SELECT COUNT(*) FROM dbo.work_orders
          WHERE plan_date = CAST(GETDATE() AS DATE)
            AND status NOT IN ('Created', 'Cancelled'))                        AS plan_released,
        (SELECT COUNT(*) FROM dbo.work_orders WHERE status = 'Created')         AS created_total,
        (SELECT COUNT(*) FROM dbo.work_orders w
          WHERE w.status = 'Created'
            AND EXISTS (SELECT 1 FROM dbo.pc_wo_routing_steps s
                         WHERE s.work_order_id = w.work_order_id))              AS routed_not_released
  """)
  return serialize_row(cursor.fetchone() or {})


def _defaults(cursor):
  """The wo_type default routes (migration 108) -> {'Standard': {...},
  'Non-Standard': {...}} in the _templates_for_parts shape. Empty before
  108 is applied - the Desk then simply shows no default source."""
  by_part = _templates_for_parts(cursor, list(DEFAULT_TEMPLATE_PARTS.values()))
  out = {}
  for key, part in DEFAULT_TEMPLATE_PARTS.items():
    if part in by_part:
      e = dict(by_part[part])
      e['wo_type'] = key
      out[key] = e
  return out


def _attach_summaries(rows, steps_by_wo, templates=None, defaults=None):
  """route_summary from the saved steps, plus the 108 expectation per row:
  expected_route_summary / expected_source ('part' | 'Standard' |
  'Non-Standard' | None) and is_exception (a saved routing that is not the
  expected one). An unrouted row is never an exception - it is 'none'."""
  for d in rows:
    steps = steps_by_wo.get(str(d['work_order_id'])) or []
    d['route_summary'] = _route_summary(steps) if steps else ''
    exp, source = _expected_route(d.get('part_number'), d.get('wo_type'), templates, defaults)
    d['expected_route_summary'] = (exp or {}).get('route_summary') or ''
    d['expected_source'] = source
    d['expected_template_id'] = (exp or {}).get('template_id')
    d['is_exception'] = 1 if (steps and exp and _route_sig(steps) != _route_sig(exp['steps'])) else 0
  return rows


def _payload(cursor, where_sql, params, top, view):
  """Shared assembly for open / search / refresh. 8 statements: count,
  rows, steps, templates, defaults, specials, departments, totals."""
  # Full notes ride inline only for a small set (one cheap COUNT decides;
  # the predicates reference wo.* only, so the count needs no joins).
  cursor.execute(f"SELECT COUNT(*) AS n FROM dbo.work_orders wo WHERE {where_sql}",
                 tuple(params))
  n = int((cursor.fetchone() or {}).get('n') or 0)
  inline = n <= INLINE_NOTES_MAX
  rows, truncated = _desk_rows(cursor, where_sql, params, top=top, inline_notes=inline)
  ids = [r['work_order_id'] for r in rows]
  steps_by_wo = _steps_for(cursor, [i for i, r in zip(ids, rows) if r['step_count'] > 0])
  parts = [r['part_number'] for r in rows]
  templates = _templates_for_parts(cursor, parts)
  defaults = _defaults(cursor)
  _attach_summaries(rows, steps_by_wo, templates, defaults)
  return {
    'status': 'success',
    'view': view,
    'rows': rows,
    'steps_by_wo': steps_by_wo,
    'templates': templates,
    'defaults': defaults,
    'specials': _specials_for_parts(cursor, parts),
    'departments': _departments(cursor),
    'totals': _totals(cursor),
    'truncated': truncated,
    'notes_inline': 1 if inline else 0,
    'row_cap': MAX_ROWS,
    'loaded_at': datetime.now().isoformat(),
  }


_VIEW_PREDICATES = {
  # Today's published plan, Created and Released, in plan order (no cap:
  # a plan is 30-60 lines). Released rows are read-only context + the chip.
  'plan':    ("wo.plan_date = CAST(GETDATE() AS DATE) AND wo.status IN ('Created', 'Released')", None),
  # The Created backlog: plan rows first, then earliest due. Capped.
  'created': ("wo.status = 'Created'", MAX_ROWS),
  # Created work orders that already carry a saved routing (routed, not released).
  'routed':  ("wo.status = 'Created' AND EXISTS (SELECT 1 FROM dbo.pc_wo_routing_steps s "
              "WHERE s.work_order_id = wo.work_order_id)", MAX_ROWS),
}


@anvil.server.callable
@perf_probe.timed
def open_routing_desk(view='plan'):
  """
    Everything the Routing Desk shows, in one call.

    view: 'plan' (default) | 'created' | 'routed'   (see _VIEW_PREDICATES)

    Returns:
        {status, view, rows[list[dict]], steps_by_wo{str(wo_id): [...]},
         templates{part_number: {...}}, defaults{'Standard'|'Non-Standard': {...}},
         specials[...], departments[...],
         totals{plan_total, plan_released, created_total, routed_not_released},
         truncated, notes_inline, row_cap, loaded_at}
    Each row also carries expected_route_summary / expected_source /
    expected_template_id / is_exception (migration 108).
  """
  if view not in _VIEW_PREDICATES:
    return {'status': 'error', 'message': f"Unknown view '{view}'"}
  where_sql, top = _VIEW_PREDICATES[view]
  conn = get_connection()
  try:
    cursor = conn.cursor()
    return _payload(cursor, where_sql, (), top, view)
  except Exception as e:
    return {'status': 'error', 'message': f'Error loading the Routing Desk: {e}'}
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def search_desk_orders(search_text):
  """
    'Search all orders' - the explicit round trip. WO#, SO#, SO-rel or part
    number, Created and Released work orders, capped at SEARCH_ROWS. Same
    payload shape as open_routing_desk; the client merges rows by work_order_id
    and never touches its local drafts.
  """
  text = (search_text or '').strip()
  if len(text) < 2:
    return {'status': 'error', 'message': 'Type at least two characters to search all orders.'}
  like = f"%{text}%"
  where_sql = """wo.status IN ('Created', 'Released')
        AND (wo.work_order_number LIKE %s
             OR wo.sales_order_number LIKE %s
             OR CONCAT(wo.sales_order_number, '-', wo.sales_order_rel) LIKE %s
             OR wo.part_number LIKE %s)"""
  conn = get_connection()
  try:
    cursor = conn.cursor()
    return _payload(cursor, where_sql, (like, like, like, like), SEARCH_ROWS, 'search')
  except Exception as e:
    return {'status': 'error', 'message': f'Search failed: {e}'}
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def refresh_desk_rows(work_order_ids):
  """
    Fresh rows + steps for specific work orders (after a save, release or
    'Refresh this order'). The client merges by work_order_id.
  """
  ids = [int(i) for i in (work_order_ids or []) if i is not None]
  if not ids:
    return {'status': 'success', 'rows': [], 'steps_by_wo': {}}
  ph = ", ".join(["%s"] * len(ids))
  conn = get_connection()
  try:
    cursor = conn.cursor()
    rows, _ = _desk_rows(cursor, f"wo.work_order_id IN ({ph})", tuple(ids),
                         top=None, inline_notes=(len(ids) <= INLINE_NOTES_MAX))
    steps_by_wo = _steps_for(cursor, [r['work_order_id'] for r in rows if r['step_count'] > 0])
    templates = _templates_for_parts(cursor, [r['part_number'] for r in rows])
    defaults = _defaults(cursor)
    _attach_summaries(rows, steps_by_wo, templates, defaults)
    return {'status': 'success', 'rows': rows, 'steps_by_wo': steps_by_wo,
            'templates': templates, 'defaults': defaults}
  except Exception as e:
    return {'status': 'error', 'message': f'Refresh failed: {e}'}
  finally:
    conn.close()


# =============================================================================
# SAVE ROUTING (Pending only - nothing goes Ready, nobody is texted)
# =============================================================================

def _load_wo(cursor, work_order_id):
  cursor.execute("""
      SELECT work_order_id, work_order_number, part_number, status, wo_type,
             sales_order_number, sales_order_rel, quantity, wo_override_qty
      FROM dbo.work_orders WHERE work_order_id = %s
  """, (work_order_id,))
  return cursor.fetchone()


def _worked_count(cursor, work_order_id):
  cursor.execute("""
      SELECT COUNT(*) AS worked FROM dbo.pc_wo_routing_steps
      WHERE work_order_id = %s
        AND (status IN (%s, %s) OR qty_completed > 0)
  """, (work_order_id, STATUS_IN_PROGRESS, STATUS_COMPLETE))
  return cursor.fetchone()['worked']


def _group_map(steps):
  """seq_group -> gapped sequence_number (10, 20, 30...). Steps sharing a
  seq_group share a sequence_number (parallel). Falls back to list order
  when a client sends no seq_group."""
  groups = [s.get('seq_group') for s in steps]
  if any(g is None for g in groups):
    groups = list(range(1, len(steps) + 1))
  gm = {g: (i + 1) * 10 for i, g in enumerate(sorted(set(groups)))}
  return groups, gm


def _conflict(cursor, work_order_id, current_version):
  """Payload for a refused save: who saved what, when."""
  steps = _steps_for(cursor, [work_order_id]).get(str(work_order_id), [])
  return {
    'status': 'conflict',
    'message': ('Someone saved a routing for this work order after you loaded it. '
                'Reload to see it, or reapply your steps on top of it.'),
    'current_version': current_version,
    'saved_by': steps[0].get('created_by') if steps else None,
    'saved_at': steps[0].get('created_at') if steps else None,
    'current_steps': steps,
    'current_route_summary': _route_summary(steps),
  }


def _save_on_cursor(cursor, wo, steps, expected_version, source_template_id, actor):
  """The save, without commit. Returns a result dict; raises nothing it can
  report. Caller commits on 'success', rolls back otherwise."""
  work_order_id = wo['work_order_id']
  if not steps:
    return {'status': 'error', 'message': 'Routing must contain at least one step'}
  for s in steps:
    if not s.get('department_id'):
      return {'status': 'error', 'message': 'Every step needs a department'}
  if wo['status'] != WO_CREATED:
    return {'status': 'error',
            'message': (f"{wo['work_order_number']} is {wo['status']} - only Created work "
                        f"orders are routed here. UnRelease it first to change its routing.")}
  if _worked_count(cursor, work_order_id) > 0:
    return {'status': 'error',
            'message': ('This routing has recorded work. Use Record Progress or Reset '
                        'for Rework instead of re-saving it.')}

  current_version = _version_token(cursor, work_order_id)
  if (expected_version or None) != (current_version or None):
    return _conflict(cursor, work_order_id, current_version)

  effective_qty = wo['wo_override_qty'] or wo['quantity']

  # Replace the prior (unworked) snapshot: events first (FK), then steps.
  cursor.execute("DELETE FROM dbo.pc_wo_routing_step_events WHERE work_order_id = %s",
                 (work_order_id,))
  cursor.execute("DELETE FROM dbo.pc_wo_routing_steps WHERE work_order_id = %s",
                 (work_order_id,))

  groups, gm = _group_map(steps)
  src = int(source_template_id) if source_template_id else None
  for s, g in zip(steps, groups):
    cursor.execute("""
        INSERT INTO dbo.pc_wo_routing_steps
            (work_order_id, sequence_number, department_id, status,
             qty_required, step_notes, source_template_id, created_by)
        OUTPUT INSERTED.wo_step_id
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (work_order_id, gm[g], int(s['department_id']), STATUS_PENDING,
          effective_qty, ((s.get('step_notes') or '').strip()[:500] or None), src, actor))
    wo_step_id = cursor.fetchone()['wo_step_id']
    cursor.execute("""
        INSERT INTO dbo.pc_wo_routing_step_events
            (wo_step_id, work_order_id, event_type, event_by, notes)
        VALUES (%s, %s, 'Edit', %s, 'desk-save')
    """, (wo_step_id, work_order_id, actor))

  # How does the saved routing relate to the library (108)?
  #   template_differs    - not the part's active template (or the part has none)
  #   is_exception        - not what sp_pc_apply_default_routing would write
  #                         (part template if any, else the wo_type default)
  #   prompt_part_default - worth asking "save as the default for this part?":
  #                         differs from the part template AND is neither the
  #                         wo_type default nor a bare A - an A-only routing is
  #                         the pre-108 habit the default now supersedes, and
  #                         saving it as a part default would re-create the 662
  #                         templates migration 108 retired.
  new_steps = _steps_for(cursor, [work_order_id]).get(str(work_order_id), [])
  saved_sig = _route_sig(new_steps)
  templates = _templates_for_parts(cursor, [wo['part_number']])
  defaults = _defaults(cursor)
  part_tpl = templates.get(wo['part_number'])
  template_differs = (not part_tpl) or (_route_sig(part_tpl['steps']) != saved_sig)
  exp, exp_source = _expected_route(wo['part_number'], wo.get('wo_type'), templates, defaults)
  is_exception = bool(exp) and _route_sig(exp['steps']) != saved_sig
  # Either library default (the order's own type or the other one - picking
  # "Default Non-Standard" for a Standard order is a one-off call, not a
  # part route) keeps the prompt quiet.
  equals_type_default = any(_route_sig(d['steps']) == saved_sig
                            for d in defaults.values() if d.get('steps'))
  a_only = (len(new_steps) == 1 and (new_steps[0].get('dept_code') or '').upper() == 'A')
  return {
    'status': 'success',
    'work_order_id': work_order_id,
    'work_order_number': wo['work_order_number'],
    'part_number': wo['part_number'],
    'version': _version_token(cursor, work_order_id),
    'step_count': len(steps),
    'template_differs': template_differs,
    'is_exception': 1 if is_exception else 0,
    'expected_route_summary': (exp or {}).get('route_summary') or '',
    'expected_source': exp_source,
    'prompt_part_default': bool(template_differs and not equals_type_default and not a_only),
    'steps': new_steps,
    'route_summary': _route_summary(new_steps),
  }


@anvil.server.callable
@perf_probe.timed
def save_routing(work_order_id, steps, expected_version=None,
                 source_template_id=None, actor=None):
  """
    Save a work order's routing WITHOUT releasing it.

    steps: [{seq_group, department_id, step_notes}, ...] in display order
           (seq_group shared = parallel; same contract as the editor).
    expected_version: the routing's version token the client loaded
           (rows[i]['routing_version'] / a previous save's 'version'), or
           None for a work order that had no routing.
    source_template_id: the template the steps were copied from, if any.

    Every step is written Pending. Returns {status:'success', version,
    step_count, template_differs, is_exception, expected_route_summary,
    expected_source, prompt_part_default, steps, route_summary} - or
    {status:'conflict', current_steps...} when the version moved - or
    {status:'error', message}. 'Routing saved; order not released.'
  """
  actor = _actor_or_default(actor)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    wo = _load_wo(cursor, work_order_id)
    if not wo:
      return {'status': 'error', 'message': f'Work order {work_order_id} not found'}
    result = _save_on_cursor(cursor, wo, steps or [], expected_version, source_template_id, actor)
    if result.get('status') == 'success':
      conn.commit()
    else:
      conn.rollback()
    return result
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error saving routing: {e}'}
  finally:
    conn.close()


# =============================================================================
# RELEASE (the only path to work_orders.status = 'Released')
# =============================================================================

def _recipient_count(cursor, token, cache):
  """Active contacts explicitly subscribed to a routing token (same rule as
  routing_notifications._recipients_for_token; counted, not fetched)."""
  if token in cache:
    return cache[token]
  cursor.execute("""
      SELECT COUNT(*) AS n FROM dbo.sms_notification_contacts
      WHERE is_active = 1 AND notification_types LIKE %s
  """, (f'%{token}%',))
  cache[token] = int((cursor.fetchone() or {}).get('n') or 0)
  return cache[token]


def _queue_ready_text(cursor, wo, step, recip_cache, released_note):
  """Queue the ONE notification rule (007): Pending -> Ready texts that
  department. A token with no active subscriber gets an audit row that is
  already Failed with attempts exhausted (review D8) - no retries."""
  token = f"ROUTING_READY_{step['dept_code'] or '?'}"
  message = (f"{wo['work_order_number']} ({wo['part_number']}) is ready for "
             f"{step['department_name']}. {released_note}")[:500]
  if _recipient_count(cursor, token, recip_cache) > 0:
    cursor.execute("""
        INSERT INTO dbo.pc_notifications
            (work_order_id, wo_step_id, notify_type, department_id, message)
        VALUES (%s, %s, 'Dept_Step_Ready', %s, %s)
    """, (wo['work_order_id'], step['wo_step_id'], step['department_id'], message))
    return True, None
  err = f'No active recipients for {token} - not queued at release'
  cursor.execute("""
      INSERT INTO dbo.pc_notifications
          (work_order_id, wo_step_id, notify_type, department_id, message,
           status, attempts, last_error)
      VALUES (%s, %s, 'Dept_Step_Ready', %s, %s, 'Failed', %s, %s)
  """, (wo['work_order_id'], step['wo_step_id'], step['department_id'], message,
        NOTIFY_MAX_ATTEMPTS, err[:300]))
  return False, err


def _log_action(cursor, wo, action, source, old_status, new_status, actor):
  """work_order_action_log row. Best-effort like the cancel audit: a missing
  table must never block a release."""
  try:
    cursor.execute("""
        INSERT INTO dbo.work_order_action_log
          (work_order_id, work_order_number, sales_order_number, sales_order_rel,
           action, source, old_status, new_status, actor)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (wo['work_order_id'], wo['work_order_number'], wo['sales_order_number'],
          wo['sales_order_rel'], action[:40], (source or '')[:40], old_status, new_status,
          (actor or '')[:150]))
  except Exception:
    pass


def _release_on_cursor(cursor, wo, actor, source, recip_cache):
  """The release, without commit. Guard -> first group Ready -> events ->
  texts -> status -> audit. Idempotent against steps that are ALREADY Ready
  (the old editor path and the external batch writer set Ready at save):
  only steps flipped here get an event and a text, so the legacy path still
  produces exactly one text per release."""
  work_order_id = wo['work_order_id']
  if wo['status'] != WO_CREATED:
    return {'status': 'error',
            'message': f"{wo['work_order_number']} is {wo['status']} - only Created work orders can be released."}
  steps = _steps_for(cursor, [work_order_id]).get(str(work_order_id), [])
  if not steps:
    return {'status': 'error',
            'message': f"{wo['work_order_number']} has no saved routing. Save a routing first."}

  # The first OPEN group is what release readies. For a fresh routing that is
  # step 1. For an order that was un-released after work was recorded (v1.0's
  # UnRelease leaves the steps as they were; auto-completed F / P stay
  # Complete) it is the first group that is not Complete / Skipped, so a
  # re-release RESUMES where the order stood instead of being refused
  # (1.2.1: six orders were un-released on 2026-10-15 and could not be
  # released again).
  open_steps = [s for s in steps if s['status'] not in (STATUS_COMPLETE, STATUS_SKIPPED)]
  if not open_steps:
    return {'status': 'error',
            'message': (f"{wo['work_order_number']}: every routing step is already complete - "
                        f"nothing to release. Mark the work order Complete instead.")}
  first_seq = min(s['sequence_number'] for s in open_steps)
  first_steps = [s for s in steps if s['sequence_number'] == first_seq]
  resumed = any(s['status'] in (STATUS_COMPLETE, STATUS_SKIPPED) for s in steps) or \
  any(s['status'] in (STATUS_READY, STATUS_IN_PROGRESS) for s in first_steps)
  readied = []
  for s in first_steps:
    if s['status'] == STATUS_PENDING:
      cursor.execute("""
          UPDATE dbo.pc_wo_routing_steps SET status = %s
          WHERE wo_step_id = %s AND status = %s
      """, (STATUS_READY, s['wo_step_id'], STATUS_PENDING))
      if cursor.rowcount:
        cursor.execute("""
            INSERT INTO dbo.pc_wo_routing_step_events
                (wo_step_id, work_order_id, event_type, event_by, notes)
            VALUES (%s, %s, 'Ready', %s, 'first step at release')
        """, (s['wo_step_id'], work_order_id, actor))
        readied.append(s)

  auto = _auto_complete_depts(cursor, recip_cache)
  texts = []
  for s in readied:
    if (s['dept_code'] or '').upper() in auto:
      texts.append({'department_name': s['department_name'], 'dept_code': s['dept_code'],
                    'queued': False, 'note': 'auto-completed at release (no board yet)'})
      continue                       # completed a few lines down - nobody to text
    sent, err = _queue_ready_text(cursor, wo, s, recip_cache,
                                  'Released by Production Control.')
    texts.append({'department_name': s['department_name'], 'dept_code': s['dept_code'],
                  'queued': bool(sent), 'note': err})

  cursor.execute("""
      UPDATE dbo.work_orders SET status = %s
      WHERE work_order_id = %s AND status = %s
  """, (WO_RELEASED, work_order_id, WO_CREATED))
  if cursor.rowcount != 1:
    return {'status': 'error',
            'message': (f"{wo['work_order_number']} changed status before it could be "
                        f"released. Refresh and try again.")}
  _log_action(cursor, wo, 'Release' if not resumed else 'Release (resume)', source, WO_CREATED, WO_RELEASED, actor)

  # Departments with no board yet (global_parameters pc_auto_complete_<code> = 1):
  # their Ready steps complete here, in the same transaction, so the order
  # lands at the first real department exactly as it did before 108.
  auto_done, next_ready = _auto_complete_on_cursor(cursor, wo, recip_cache, auto)

  return {
    'status': 'success',
    'work_order_id': work_order_id,
    'work_order_number': wo['work_order_number'],
    'new_status': WO_RELEASED,
    'first_steps': [{'department_name': s['department_name'], 'dept_code': s['dept_code'],
                     'already_ready': (s not in readied)} for s in first_steps],
    'texts': texts,
    'auto_completed': auto_done,
    'next_ready': next_ready,
    'resumed': bool(resumed),
    'route_summary': _route_summary(steps),
    'released_at': datetime.now().isoformat(),
  }


# -----------------------------------------------------------------------------
# AUTO-COMPLETE (temporary: departments that have no board yet)
# -----------------------------------------------------------------------------
# dbo.global_parameters rows  pc_auto_complete_<dept_code> = 1  (migration 111:
# F Material Handling and P Pre-Assembly). While a row is 1, a Ready step at
# that department is completed by this module the moment it goes Ready at
# release (actor AUTO_ACTOR, honest event note), with the PC text suppressed.
# To retire it for a department: set its row to 0 (or delete it). No code
# change, no deploy. Remove this block and the three call sites once both
# boards exist.
AUTO_ACTOR = 'AUTO (no board yet)'
AUTO_NOTE = 'auto-completed at release - no board for this department yet (global_parameters pc_auto_complete_*)'


def _auto_complete_depts(cursor, cache):
  """Set of dept codes flagged for auto-complete. Cached per request."""
  if '__auto__' in cache:
    return cache['__auto__']
  codes = set()
  try:
    cursor.execute("""
        SELECT parameter_name FROM dbo.global_parameters
        WHERE parameter_name LIKE 'pc_auto_complete_%' AND parameter_value = 1
    """)
    for r in cursor.fetchall():
      code = (r['parameter_name'] or '').rsplit('_', 1)[-1].strip().upper()
      if len(code) == 1:
        codes.add(code)
  except Exception:
    codes = set()                    # no parameters -> nothing auto-completes
  cache['__auto__'] = codes
  return codes


def _d8_after_proc(cursor, work_order_id, wo_step_id, recip_cache, suppress_pc=True,
                   reason='step completed by Production Control from the Desk'):
  """Review D8 applied to what sp_pc_complete_step just queued, in the
  caller's transaction: PC's own step-complete text is suppressed when PC
  (or the auto-complete) did the step; a Dept_Step_Ready for a department
  with no subscriber becomes one Failed audit row, no retries. Returns the
  newly Ready departments [{department_name, dept_code, texted}]."""
  if suppress_pc:
    cursor.execute("""
        UPDATE dbo.pc_notifications
        SET status = 'Failed', attempts = %s, last_error = %s
        WHERE wo_step_id = %s AND notify_type = 'PC_Step_Complete' AND status = 'Queued'
    """, (NOTIFY_MAX_ATTEMPTS, ('Suppressed - ' + reason)[:300], wo_step_id))
  cursor.execute("""
      SELECT n.notification_id, n.department_id, d.dept_code, d.department_name
      FROM dbo.pc_notifications n
      JOIN dbo.pc_departments d ON d.department_id = n.department_id
      WHERE n.work_order_id = %s AND n.notify_type = 'Dept_Step_Ready' AND n.status = 'Queued'
      ORDER BY n.notification_id
  """, (work_order_id,))
  out = []
  for n in cursor.fetchall():
    token = f"ROUTING_READY_{n['dept_code'] or '?'}"
    texted = _recipient_count(cursor, token, recip_cache) > 0
    if not texted:
      cursor.execute("""
          UPDATE dbo.pc_notifications
          SET status = 'Failed', attempts = %s, last_error = %s
          WHERE notification_id = %s AND status = 'Queued'
      """, (NOTIFY_MAX_ATTEMPTS,
            f'No active recipients for {token} - not queued at completion'[:300],
            n['notification_id']))
    out.append({'department_name': n['department_name'], 'dept_code': n['dept_code'],
                'texted': bool(texted)})
  return out


def _auto_complete_on_cursor(cursor, wo, recip_cache, auto):
  """Complete every Ready step at an auto-complete department, following the
  cascade (F done -> P Ready -> P done -> B/A Ready). Stops at the first
  department that is not flagged. Returns (completed[{department_name,
  dept_code}], next_ready[...])."""
  done, next_ready = [], []
  if not auto:
    return done, next_ready
  for _ in range(12):                                   # bounded: a routing is a handful of steps
    cursor.execute("""
        SELECT s.wo_step_id, s.qty_required, s.qty_completed, d.dept_code, d.department_name
        FROM dbo.pc_wo_routing_steps s
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        WHERE s.work_order_id = %s AND s.status = %s
        ORDER BY s.sequence_number, s.wo_step_id
    """, (wo['work_order_id'], STATUS_READY))
    ready = [r for r in cursor.fetchall() if (r['dept_code'] or '').upper() in auto]
    if not ready:
      break
    progressed = False
    for r in ready:
      remaining = int(r['qty_required'] or 0) - int(r['qty_completed'] or 0)
      if remaining <= 0:
        continue
      cursor.execute("""
          EXEC dbo.sp_pc_complete_step @wo_step_id=%s, @qty=%s, @actor=%s, @notes=%s
      """, (r['wo_step_id'], remaining, AUTO_ACTOR, AUTO_NOTE[:500]))
      proc = cursor.fetchone() or {}
      if proc.get('status') != 'success':
        raise RuntimeError(f"auto-complete of {r['department_name']} failed: {proc.get('message')}")
      # The auto-completion queued a Dept_Step_Ready for whatever went Ready
      # next and a PC_Step_Complete nobody needs; D8 both.
      next_ready = _d8_after_proc(cursor, wo['work_order_id'], r['wo_step_id'], recip_cache,
                                  reason='step auto-completed at release (no board yet)')
      done.append({'department_name': r['department_name'], 'dept_code': r['dept_code']})
      progressed = True
    if not progressed:
      break
  # Only departments still Ready after the loop are "next" (a flagged one was completed)
  next_ready = [n for n in next_ready if (n['dept_code'] or '').upper() not in auto]
  return done, next_ready


@anvil.server.callable
@perf_probe.timed
def release_work_order(work_order_id, actor=None, source='desk'):
  """
    Release ONE work order to the floor: first routing group -> Ready (Ready
    events, department texts), work_orders.status Created -> Released, action
    log. Refuses a work order with no saved routing. One transaction.

    source: 'desk' | 'desk-batch' | 'legacy' | 'plan' (audit only)

    Returns {status, first_steps[], texts[], route_summary, released_at}
    or {status:'error', message}.
  """
  actor = _actor_or_default(actor)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    wo = _load_wo(cursor, work_order_id)
    if not wo:
      return {'status': 'error', 'message': f'Work order {work_order_id} not found'}
    result = _release_on_cursor(cursor, wo, actor, source, {})
    if result.get('status') == 'success':
      conn.commit()
    else:
      conn.rollback()
    return result
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error releasing work order: {e}'}
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def release_work_orders(work_order_ids, actor=None, source='desk-batch'):
  """
    Release several work orders: ONE connection, one transaction PER work
    order, so partial success is reported honestly. Returns per-order
    results in the order given plus a summary.

    Returns {status:'success', released, failed, results:[{work_order_id,
    status, message|first_steps...}]}
  """
  actor = _actor_or_default(actor)
  ids = [int(i) for i in (work_order_ids or []) if i is not None]
  results, released, failed = [], 0, 0
  if not ids:
    return {'status': 'success', 'released': 0, 'failed': 0, 'results': []}
  if len(ids) > BATCH_MAX:
    return {'status': 'error',
            'message': f'Release at most {BATCH_MAX} work orders per call (got {len(ids)}); '
            f'send the rest in another call.'}
  conn = get_connection()
  try:
    cursor = conn.cursor()
    recip_cache = {}
    for wid in ids:
      try:
        wo = _load_wo(cursor, wid)
        if not wo:
          r = {'status': 'error', 'message': f'Work order {wid} not found'}
        else:
          r = _release_on_cursor(cursor, wo, actor, source, recip_cache)
        if r.get('status') == 'success':
          conn.commit()
          released += 1
        else:
          conn.rollback()
          failed += 1
      except Exception as e:
        conn.rollback()
        r = {'status': 'error', 'message': f'Error: {e}'}
        failed += 1
      r['work_order_id'] = wid
      results.append(r)
    return {'status': 'success', 'released': released, 'failed': failed, 'results': results}
  finally:
    conn.close()


@anvil.server.callable
@perf_probe.timed
def save_and_release_work_order(work_order_id, steps, expected_version=None,
                                source_template_id=None, actor=None):
  """
    The Desk's loud button: save the routing, then release - two
    transactions on one connection, so a release failure never loses the
    routing. Returns {'status', 'save': {...}, 'release': {...}}; status is
    'success' only when both succeeded ('saved_only' when the save landed
    and the release did not).
  """
  actor = _actor_or_default(actor)
  conn = get_connection()
  try:
    cursor = conn.cursor()
    wo = _load_wo(cursor, work_order_id)
    if not wo:
      return {'status': 'error', 'message': f'Work order {work_order_id} not found'}
    saved = _save_on_cursor(cursor, wo, steps or [], expected_version, source_template_id, actor)
    if saved.get('status') != 'success':
      conn.rollback()
      return {'status': saved.get('status', 'error'), 'save': saved, 'release': None,
              'message': saved.get('message')}
    conn.commit()
    try:
      released = _release_on_cursor(cursor, wo, actor, 'desk', {})
      if released.get('status') == 'success':
        conn.commit()
        return {'status': 'success', 'save': saved, 'release': released}
      conn.rollback()
      return {'status': 'saved_only', 'save': saved, 'release': released,
              'message': f"Routing saved; release failed: {released.get('message')}"}
    except Exception as e:
      conn.rollback()
      return {'status': 'saved_only', 'save': saved,
              'release': {'status': 'error', 'message': str(e)},
              'message': f'Routing saved; release failed: {e}'}
  except Exception as e:
    conn.rollback()
    return {'status': 'error', 'message': f'Error: {e}'}
  finally:
    conn.close()


# =============================================================================
# DESK STEP COMPLETION (Material Handling / Pre-Assembly - no board of their own)
# =============================================================================

def _with_dims(note, dims):
  """'Fabricate special straining element - Dims: 2.95 x 0.75' - the
  original note kept, one Dims segment (replaced when already present).
  ASCII separator on purpose: step_notes is VARCHAR and every board
  renders it."""
  base = (note or '').strip()
  if DIMS_TAG in base:
    base = base.split(DIMS_TAG, 1)[0].rstrip(' -').strip()
  seg = f"{DIMS_TAG} {dims}"
  return (f"{base} - {seg}" if base else seg)[:500]


def _write_dims(cursor, step, dims, actor):
  """Element dimensions documented at Pre-Assembly go into THIS step's notes
  and every LATER step's notes on the work order, because each department
  board reads only its own step's notes (vw_pc_department_queue.step_notes).
  One Edit event per touched step. Returns the touched wo_step_ids."""
  cursor.execute("""
      SELECT wo_step_id, sequence_number, step_notes
      FROM dbo.pc_wo_routing_steps
      WHERE work_order_id = %s AND sequence_number >= %s
        AND status NOT IN (%s, %s)
      ORDER BY sequence_number, wo_step_id
  """, (step['work_order_id'], step['sequence_number'], STATUS_COMPLETE, STATUS_SKIPPED))
  touched = []
  for r in cursor.fetchall():
    new_note = _with_dims(r['step_notes'], dims)
    if new_note == (r['step_notes'] or ''):
      continue
    cursor.execute("UPDATE dbo.pc_wo_routing_steps SET step_notes = %s WHERE wo_step_id = %s",
                   (new_note, r['wo_step_id']))
    cursor.execute("""
        INSERT INTO dbo.pc_wo_routing_step_events
            (wo_step_id, work_order_id, event_type, event_by, notes)
        VALUES (%s, %s, 'Edit', %s, %s)
    """, (r['wo_step_id'], step['work_order_id'], actor,
          f"dimensions documented from the Desk: {dims}"[:500]))
    touched.append(r['wo_step_id'])
  return touched


@anvil.server.callable
@perf_probe.timed
def complete_desk_step(wo_step_id, actor=None, dims=None, note=None):
  """
    Production Control completes a Material Handling or Pre-Assembly step
    from the Desk ("Material staged" / "Dimensions documented"). Those two
    departments have no board (108), so PC is the operator for them.

    Guards: the work order is Released; the step's department is F or P;
    the step is Ready or In Progress (a Pending step belongs to an order
    whose earlier step is not done - the Desk never jumps the sequence).
    dims (optional, Pre-Assembly): element dimensions, written into this
    step's notes and every later step's notes so the next boards see them.

    The completion itself is sp_pc_complete_step (the single write path:
    qty, Complete event, Ready cascade, department text, WO auto-complete).
    Then review D8 is applied to what the proc queued, in the same
    transaction: PC's own PC_Step_Complete text is suppressed (PC did it),
    and a Dept_Step_Ready text for a department with no subscriber becomes
    an audit row (Failed, attempts exhausted) instead of three retries.

    Returns {status:'success', work_order_id, work_order_number, step{...},
             step_status, wo_complete, next_ready[{department_name, dept_code,
             texted}], dims_written, steps[...], route_summary}
    or {status:'error', message}.
  """
  actor = _actor_or_default(actor)
  dims_text = ' '.join((dims or '').split())[:200]
  conn = get_connection()
  try:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.wo_step_id, s.work_order_id, s.sequence_number, s.department_id,
               d.dept_code, d.department_name, s.status, s.qty_required, s.qty_completed,
               s.step_notes,
               w.work_order_number, w.part_number, w.status AS wo_status,
               w.sales_order_number, w.sales_order_rel
        FROM dbo.pc_wo_routing_steps s
        JOIN dbo.pc_departments d ON d.department_id = s.department_id
        JOIN dbo.work_orders w ON w.work_order_id = s.work_order_id
        WHERE s.wo_step_id = %s
    """, (wo_step_id,))
    step = cursor.fetchone()
    if not step:
      return {'status': 'error', 'message': f'Routing step {wo_step_id} not found'}
    if step['wo_status'] != WO_RELEASED:
      return {'status': 'error',
              'message': (f"{step['work_order_number']} is {step['wo_status']} - steps are "
                          f"completed from the Desk only while the order is Released.")}
    if (step['dept_code'] or '').upper() not in DESK_COMPLETABLE_DEPTS:
      return {'status': 'error',
              'message': (f"{step['department_name']} completes its own steps from its board; "
                          f"the Desk only completes Material Handling and Pre-Assembly.")}
    if step['status'] not in (STATUS_READY, STATUS_IN_PROGRESS):
      why = {STATUS_PENDING: 'an earlier step is not done yet',
             STATUS_COMPLETE: 'it is already complete',
             STATUS_SKIPPED: 'it was skipped'}.get(step['status'], step['status'])
      return {'status': 'error',
              'message': f"{step['department_name']} step is {step['status']} - {why}."}
    remaining = int(step['qty_required'] or 0) - int(step['qty_completed'] or 0)
    if remaining <= 0:
      return {'status': 'error', 'message': f"{step['department_name']} step is already complete."}

    dims_written = _write_dims(cursor, step, dims_text, actor) if dims_text else []

    proc_note = ' '.join((note or '').split())[:500] or \
    f"completed from the Routing Desk ({step['department_name']})"
    cursor.execute("""
        EXEC dbo.sp_pc_complete_step @wo_step_id=%s, @qty=%s, @actor=%s, @notes=%s
    """, (wo_step_id, remaining, actor, proc_note))
    proc = cursor.fetchone() or {}
    if proc.get('status') != 'success':
      conn.rollback()
      return {'status': 'error', 'message': proc.get('message') or 'Completion failed'}

    # D8 on the proc's queue (same transaction): PC's own step-complete text
    # is noise when PC clicked the button; recipient-less departments get one
    # Failed audit row instead of three retries.
    next_ready = _d8_after_proc(cursor, step['work_order_id'], wo_step_id, {})
    conn.commit()

    steps = _steps_for(cursor, [step['work_order_id']]).get(str(step['work_order_id']), [])
    return {
      'status': 'success',
      'work_order_id': step['work_order_id'],
      'work_order_number': step['work_order_number'],
      'step': {'wo_step_id': wo_step_id, 'department_name': step['department_name'],
               'dept_code': step['dept_code'], 'sequence_number': step['sequence_number']},
      'step_status': proc.get('step_status') or STATUS_COMPLETE,
      'wo_complete': int(proc.get('wo_complete') or 0),
      'next_ready': next_ready,
      'dims_written': len(dims_written),
      'steps': steps,
      'route_summary': _route_summary(steps),
    }
  except Exception as e:
    try:
      conn.rollback()
    except Exception:
      pass
    return {'status': 'error', 'message': f'Error completing step: {e}'}
  finally:
    conn.close()


# =============================================================================
# UNRELEASE SUPPORT (called by work_order_dashboard / work_order_packet_pdf)
# =============================================================================

def demote_ready_steps(cursor, work_order_id, actor, note='un-released'):
  """
    Put a work order's Ready steps back to Pending (with an Edit event each)
    and park any Dept_Step_Ready text still Queued for it, so an un-released
    work order disappears from every department queue instead of sitting
    there as ready work (review F1 / 09-11 M5). Caller owns the transaction.
    Returns the number of steps demoted.
  """
  cursor.execute("""
      SELECT wo_step_id FROM dbo.pc_wo_routing_steps
      WHERE work_order_id = %s AND status = %s
  """, (work_order_id, STATUS_READY))
  ids = [r['wo_step_id'] for r in cursor.fetchall()]
  if not ids:
    return 0
  ph = ", ".join(["%s"] * len(ids))
  cursor.execute(f"""
      UPDATE dbo.pc_wo_routing_steps SET status = %s
      WHERE wo_step_id IN ({ph})
  """, (STATUS_PENDING,) + tuple(ids))
  for sid in ids:
    cursor.execute("""
        INSERT INTO dbo.pc_wo_routing_step_events
            (wo_step_id, work_order_id, event_type, event_by, notes)
        VALUES (%s, %s, 'Edit', %s, %s)
    """, (sid, work_order_id, (actor or 'Production')[:50], (note or 'un-released')[:500]))
  # A text queued in the last minute but not yet sent must not go out.
  cursor.execute("""
      UPDATE dbo.pc_notifications
      SET status = 'Failed', attempts = %s,
          last_error = 'Un-released before the text was sent'
      WHERE work_order_id = %s AND status = 'Queued' AND notify_type = 'Dept_Step_Ready'
  """, (NOTIFY_MAX_ATTEMPTS, work_order_id))
  return len(ids)
