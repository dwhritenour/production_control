from ._anvil_designer import RoutingStepRow2Template
from anvil import *


class RoutingStepRow2(RoutingStepRow2Template):
  """
    One routing step inside sku_details' rp_steps (Routing v2).

    item = {index, seq, department_id, step_notes, dd_items, step_ref,
            on_change, on_remove}
    tb_seq is THE parallel control: give two rows the same number and
    they run in parallel.

    Structural changes call the parent's callbacks directly (on_change /
    on_remove, passed in the item dict). Callbacks, NOT parent.raise_event:
    this HTML runtime nests wrapper containers between row and
    RepeatingPanel, so event bubbling up the parent chain is unreliable —
    the same trap as the 2026-06-30 card-grid issue.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item

    self.tb_seq.text = item['seq']
    # Placeholder instead of Anvil's "<invalid value>" when nothing is
    # selected yet (fresh + Add Step rows).
    self.dd_department.include_placeholder = True
    self.dd_department.placeholder = 'Select department…'
    self.dd_department.items = item['dd_items']
    self.dd_department.selected_value = item['department_id']
    self.tb_step_notes.text = item['step_notes']
    self.lk_remove.foreground = '#C62828'   # dark red

    # tb_seq commits on lost_focus + Enter (NOT change): the parent
    # rebuilds rp_steps.items on every structural change, and rebuilding
    # per keystroke would steal focus mid-edit.
    self.tb_seq.set_event_handler('lost_focus', self._seq_done)
    self.tb_seq.set_event_handler('pressed_enter', self._seq_done)
    self.dd_department.set_event_handler('change', self._dept_change)
    self.tb_step_notes.set_event_handler('change', self._notes_change)
    self.lk_remove.set_event_handler('click', self._remove_click)

  def _seq_done(self, **event_args):
    try:
      n = int(self.tb_seq.text)
    except (TypeError, ValueError):
      n = 0
    if n < 1:
      Notification("Step number must be 1 or higher.").show()
      self.tb_seq.text = self.item['seq']
      return
    if n != self.item['step_ref']['seq']:
      self.item['step_ref']['seq'] = n
      self.item['on_change']()

  def _dept_change(self, **event_args):
    self.item['step_ref']['department_id'] = self.dd_department.selected_value
    self.item['on_change']()   # diagram redraws with the department name

  def _notes_change(self, **event_args):
    self.item['step_ref']['step_notes'] = self.tb_step_notes.text or ''

  def _remove_click(self, **event_args):
    self.item['on_remove'](self.item['index'])
