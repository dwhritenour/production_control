from ._anvil_designer import RoutingStepRowTemplate
from anvil import *


class RoutingStepRow(RoutingStepRowTemplate):
  """
    One editable routing step inside routing_editor's rp_steps.

    item = {index, display_seq, department_id, step_notes, dd_items, step_ref}
    step_ref points at the parent's working-list dict: dropdown/notes edits
    mutate it directly (no refresh needed); move/remove raise x-events that
    the parent handles by reordering its list and rebuilding items.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item

    self.lbl_seq.text = f"{item['display_seq']}."
    self.dd_department.items = item['dd_items']
    self.dd_department.selected_value = item['department_id']
    self.tb_step_notes.text = item['step_notes']
    self.lk_remove.foreground = '#C62828'  # dark red

    self.dd_department.set_event_handler('change', self._dept_change)
    self.tb_step_notes.set_event_handler('change', self._notes_change)
    self.lk_move_up.set_event_handler('click', self._move_up_click)
    self.lk_move_down.set_event_handler('click', self._move_down_click)
    self.lk_remove.set_event_handler('click', self._remove_click)

  def _dept_change(self, **event_args):
    self.item['step_ref']['department_id'] = self.dd_department.selected_value

  def _notes_change(self, **event_args):
    self.item['step_ref']['step_notes'] = self.tb_step_notes.text or ''

  def _move_up_click(self, **event_args):
    self.parent.raise_event('x-move-up', index=self.item['index'])

  def _move_down_click(self, **event_args):
    self.parent.raise_event('x-move-down', index=self.item['index'])

  def _remove_click(self, **event_args):
    self.parent.raise_event('x-remove', index=self.item['index'])
