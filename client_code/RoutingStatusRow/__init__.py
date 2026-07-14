from ._anvil_designer import RoutingStatusRowTemplate
from anvil import *


# Status chip colors: (background, foreground). The status WORD is always
# shown — color is reinforcement only, never the sole signal.
STATUS_COLORS = {
  'Pending':     ('#ECEFF1', '#444444'),  # very pale gray-blue / dark gray
  'Ready':       ('#E3F2FD', '#1565C0'),  # pale sky blue / medium blue
  'In Progress': ('#FFF8E1', '#B45309'),  # pale warm amber / dark amber
  'Complete':    ('#E8F5E9', '#1B5E20'),  # very pale green / dark green
  'Skipped':     ('#F5F5F5', '#757575'),  # flat light gray / medium gray
}


class RoutingStatusRow(RoutingStatusRowTemplate):
  """One routing step inside routing_status's rp_status_steps (read-only,
  plus the Record… link for steps that can take progress)."""

  def __init__(self, **properties):
    self.init_components(**properties)
    item = self.item
    status = item.get('status', 'Pending')

    self.lbl_step_name.text = (
      f"{item['display_seq']}. {item.get('department_name', '')}")
    self.lbl_step_name.bold = (status == 'In Progress')

    bg, fg = STATUS_COLORS.get(status, STATUS_COLORS['Pending'])
    self.lbl_step_status.text = status
    self.lbl_step_status.background = bg
    self.lbl_step_status.foreground = fg
    self.lbl_step_status.italic = (status == 'Skipped')

    if status != 'Pending':
      self.lbl_qty_progress.text = (
        f"{item.get('qty_completed', 0)} / {item.get('qty_required', 0)}")
      self.lbl_qty_progress.visible = True

    if item.get('has_oos'):
      self.lbl_oos_flag.foreground = '#B45309'  # dark amber
      self.lbl_oos_flag.visible = True

    notes = item.get('step_notes') or ''
    self.txt_step_notes.text = notes
    self.txt_step_notes.foreground = '#595959'  # medium gray

    # Record… is offered on any step that can still take progress. Pending
    # included on purpose: recording there is the out-of-sequence case,
    # which is confirmed-and-flagged by the parent, never blocked.
    self.lk_record.visible = status in ('Pending', 'Ready', 'In Progress')
    self.lk_record.set_event_handler('click', self._record_click)

  def _record_click(self, **event_args):
    self.parent.raise_event('x-record', wo_step_id=self.item['wo_step_id'])
