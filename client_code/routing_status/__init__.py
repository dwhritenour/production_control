from ._anvil_designer import routing_statusTemplate
from anvil import *
import anvil.server


class routing_status(routing_statusTemplate):
  """
    Read-only routing progress panel + Record Progress / Rework actions.
    Embedded at the bottom of WorkOrderDetail for Released-and-later WOs.

    Raises 'x-routing-changed' after any successful record/reset so the
    host form can react (e.g. when all steps are complete).
  """

  def __init__(self, work_order_id, show_rework=False, **properties):
    self.init_components(**properties)
    self._work_order_id = work_order_id
    self._show_rework = show_rework
    self._steps = []

    self.btn_rework.set_event_handler('click', self._rework_click)
    self.rp_status_steps.set_event_handler('x-record', self._record)

    self.refresh()

  # ---------------------------------------------------------------
  # Load / refresh
  # ---------------------------------------------------------------
  def refresh(self):
    result = anvil.server.call('get_wo_routing', self._work_order_id)
    if result.get('status') != 'success':
      alert(f"Error loading routing: {result.get('message', 'Unknown error')}")
      return

    self._steps = result.get('steps', [])
    for i, s in enumerate(self._steps):
      s['display_seq'] = i + 1
    self.rp_status_steps.items = self._steps

    oos = result.get('oos_count', 0)
    if oos:
      self.lbl_oos_banner.text = (
        f"⚑ {oos} out-of-sequence event(s) — see flagged steps")
      self.lbl_oos_banner.background = '#FFF3E0'  # pale orange
      self.lbl_oos_banner.foreground = '#B45309'  # dark amber
      self.lbl_oos_banner.visible = True
    else:
      self.lbl_oos_banner.visible = False

    any_complete = any(s['status'] == 'Complete' for s in self._steps)
    self.btn_rework.visible = self._show_rework and any_complete

  # ---------------------------------------------------------------
  # Record Progress (raised by RoutingStatusRow's lk_record)
  # ---------------------------------------------------------------
  def _record(self, wo_step_id, **event_args):
    step = next(
      (s for s in self._steps if s['wo_step_id'] == wo_step_id), None)
    if step is None:
      return

    # Record-and-flag, never block: warn when earlier steps aren't done.
    earlier_open = any(
      s['sequence_number'] < step['sequence_number']
      and s['status'] not in ('Complete', 'Skipped')
      for s in self._steps)
    if earlier_open:
      if not confirm(
        "Earlier steps are not complete. Record anyway?\n\n"
        "This will be flagged as out of sequence."
      ):
        return

    remaining = max(step['qty_required'] - step['qty_completed'], 0)
    tb_qty = TextBox(type='number',
                     placeholder='Qty completed at this step',
                     text=str(remaining) if remaining else '')
    tb_note = TextBox(placeholder='Note (optional)')
    panel = ColumnPanel()
    panel.add_component(Label(
      text=(f"{step['department_name']} — "
            f"{step['qty_completed']} / {step['qty_required']} recorded so far"),
      bold=True))
    panel.add_component(tb_qty)
    panel.add_component(tb_note)

    choice = alert(content=panel,
                   title=f"Record progress — step {step['display_seq']}",
                   buttons=[("Record Qty", 'qty'),
                            ("Mark Started", 'start'),
                            ("Cancel", None)])

    if choice == 'qty':
      try:
        qty = int(tb_qty.text)
      except (TypeError, ValueError):
        alert("Enter a whole-number quantity.")
        return
      if qty <= 0:
        alert("Quantity must be greater than zero.")
        return
      result = anvil.server.call('record_step_event', wo_step_id,
                                 'Complete', qty, None,
                                 (tb_note.text or None))
    elif choice == 'start':
      result = anvil.server.call('record_step_event', wo_step_id,
                                 'Start', None, None,
                                 (tb_note.text or None))
    else:
      return

    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return

    if result.get('wo_complete'):
      Notification(
        "All routing steps are complete on this work order.",
        timeout=6).show()
    self.refresh()
    self.raise_event('x-routing-changed')

  # ---------------------------------------------------------------
  # Rework (Reopen path): PC picks the re-entry step
  # ---------------------------------------------------------------
  def _rework_click(self, **event_args):
    candidates = [s for s in self._steps if s['status'] != 'Skipped']
    if not candidates:
      return
    dd = DropDown(items=[
      (f"{s['display_seq']}. {s['department_name']}", s['sequence_number'])
      for s in candidates])
    panel = ColumnPanel()
    panel.add_component(Label(
      text="Work resumes at this step. It and every later step reset to "
      "zero; earlier steps and all history are kept.", bold=True))
    panel.add_component(dd)

    if not alert(content=panel, title="Reset steps for rework",
                 buttons=[("Reset", True), ("Cancel", False)]):
      return
    if dd.selected_value is None:
      alert("Pick the re-entry step.")
      return

    result = anvil.server.call('reset_steps_for_rework',
                               self._work_order_id, dd.selected_value)
    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return
    Notification(f"{result.get('reset_count', 0)} step(s) reset for rework").show()
    self.refresh()
    self.raise_event('x-routing-changed')
