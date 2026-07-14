from ._anvil_designer import routing_editorTemplate
from anvil import *
import anvil.server


class routing_editor(routing_editorTemplate):
  """
    Release-time routing editor dialog (Routing v1).

    Shown via:  alert(content=routing_editor(payload), large=True, buttons=[])
    where payload comes from server call get_routing_for_release(work_order_id).

    Closes itself with raise_event('x-close-alert', value=...):
      {'saved': True, 'steps': [...], 'part_number': ...}  on Save
      None                                                 on Cancel
    The CALLER flips the WO to 'Released' only when value['saved'] is True —
    a WO cannot reach Released without a saved routing.
  """

  def __init__(self, payload, **properties):
    self.init_components(**properties)

    wo = payload.get('wo', {})
    self._work_order_id = wo.get('work_order_id')
    self._part_number = wo.get('part_number', '')

    # Departments for the row dropdowns (one server call, shared by all rows)
    self._dd_items = [
      (d['department_name'], d['department_id'])
      for d in anvil.server.call('get_departments')
    ]

    # ---- Header ----
    self.lbl_editor_title.text = f"Routing — WO {wo.get('work_order_number', '')}"
    due = wo.get('wo_due_date') or wo.get('due_date') or 'n/a'
    self.lbl_wo_summary.text = (
      f"{self._part_number}  |  Qty {wo.get('effective_qty', '')}  |  Due {due}")
    self.lbl_wo_summary.foreground = '#595959'  # medium gray

    source = payload.get('source', 'none')
    if source == 'wo':
      self.lbl_template_banner.text = (
        "Loaded this work order's previous routing — review and save.")
      self.lbl_template_banner.background = '#E3F2FD'  # pale blue
    elif source == 'template':
      self.lbl_template_banner.text = (
        f"Loaded default routing for {self._part_number}.")
      self.lbl_template_banner.background = '#E3F2FD'  # pale blue
    else:
      self.lbl_template_banner.text = (
        f"No default routing exists for {self._part_number} — build it below.")
      self.lbl_template_banner.background = '#FFF8E1'  # pale amber

    # ---- Notes (deviations live here — always show when present) ----
    sku_notes = (wo.get('notes') or '').strip()
    so_notes = (wo.get('so_notes') or '').strip()
    self.ta_sku_notes.text = sku_notes
    self.lbl_sku_notes_cap.visible = bool(sku_notes)
    self.ta_sku_notes.visible = bool(sku_notes)
    self.ta_so_notes.text = so_notes
    self.lbl_so_notes_cap.visible = bool(so_notes)
    self.ta_so_notes.visible = bool(so_notes)

    # ---- Action buttons ----
    self.btn_save_routing.background = '#1565C0'  # medium blue
    self.btn_save_routing.foreground = '#FFFFFF'  # white

    # ---- Working list: plain dicts, persisted only on Save ----
    self._steps = [
      {'department_id': s['department_id'],
       'step_notes': (s.get('step_notes') or '')}
      for s in payload.get('steps', [])
    ]

    # ---- Events ----
    self.btn_add_step.set_event_handler('click', self._add_step_click)
    self.btn_save_routing.set_event_handler('click', self._save_click)
    self.btn_cancel.set_event_handler('click', self._cancel_click)
    self.rp_steps.set_event_handler('x-move-up', self._move_up)
    self.rp_steps.set_event_handler('x-move-down', self._move_down)
    self.rp_steps.set_event_handler('x-remove', self._remove)

    self._refresh()

  # ---------------------------------------------------------------
  # Working-list display
  # ---------------------------------------------------------------
  def _refresh(self):
    """Rebuild rp_steps.items from the working list (re-numbers display)."""
    self.rp_steps.items = [
      {'index': i,
       'display_seq': i + 1,
       'department_id': s['department_id'],
       'step_notes': s['step_notes'],
       'dd_items': self._dd_items,
       'step_ref': s}
      for i, s in enumerate(self._steps)
    ]
    self.btn_save_routing.enabled = bool(self._steps)

  # ---------------------------------------------------------------
  # Row events (raised by RoutingStepRow)
  # ---------------------------------------------------------------
  def _move_up(self, index, **event_args):
    if index > 0:
      self._steps[index - 1], self._steps[index] = (
        self._steps[index], self._steps[index - 1])
      self._refresh()

  def _move_down(self, index, **event_args):
    if index < len(self._steps) - 1:
      self._steps[index + 1], self._steps[index] = (
        self._steps[index], self._steps[index + 1])
      self._refresh()

  def _remove(self, index, **event_args):
    del self._steps[index]
    self._refresh()

  # ---------------------------------------------------------------
  # Buttons
  # ---------------------------------------------------------------
  def _add_step_click(self, **event_args):
    self._steps.append({'department_id': None, 'step_notes': ''})
    self._refresh()

  def _cancel_click(self, **event_args):
    self.raise_event('x-close-alert', value=None)

  def _save_click(self, **event_args):
    # Validation: every step needs a department
    if not self._steps:
      alert("Add at least one routing step.")
      return
    if any(not s['department_id'] for s in self._steps):
      alert("Every step needs a department selected.")
      return

    # Non-blocking sanity check: same department twice in a row is legal
    # (e.g. Testing after rework) but usually a mis-click.
    for i in range(1, len(self._steps)):
      if self._steps[i]['department_id'] == self._steps[i - 1]['department_id']:
        if not confirm(
          "Two consecutive steps use the same department. "
          "That's allowed — save anyway?"
        ):
          return
        break

    self.btn_save_routing.enabled = False
    self.btn_save_routing.text = "Saving..."
    try:
      result = anvil.server.call(
        'save_wo_routing', self._work_order_id, self._steps)
    finally:
      self.btn_save_routing.enabled = True
      self.btn_save_routing.text = "Save Routing & Release"

    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return

    # Capture-on-first-release / keep-the-library-current prompt
    if result.get('template_differs'):
      if confirm(
        f"Save this routing as the new default for {self._part_number}?\n\n"
        "Answer No for a one-off deviation on this order only."
      ):
        tpl = anvil.server.call(
          'save_routing_template', self._part_number, self._steps)
        if tpl.get('status') != 'success':
          alert(f"Routing saved, but the default template was not: "
                f"{tpl.get('message', 'Unknown error')}")

    self.raise_event('x-close-alert', value={
      'saved': True,
      'steps': [dict(s) for s in self._steps],
      'part_number': self._part_number,
    })
