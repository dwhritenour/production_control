from ._anvil_designer import ChangeHistoryDetailTemplate
from anvil import *
import anvil.server


class ChangeHistoryDetail(ChangeHistoryDetailTemplate):
  def __init__(self, item, **properties):
    """Popup form to display change history for a work order.

    Args:
      item: dict — the work order row data from the grid
    """
    self.init_components(**properties)
    self._item = item
    self._populate()


  def _populate(self):
    """Fetch and display change history."""
    item = self._item

    # Header
    wo_num = item.get('work_order_number', '')
    rel = item.get('sales_order_rel') or '001'
    so_num = item.get('sales_order_number', '')
    self.lbl_header.text = (
      f"Change History — {wo_num}  |  SO {so_num}-{rel}"
    )

    # Fetch changes from server
    result = anvil.server.call(
      'get_work_order_changes',
      item['work_order_id']
    )

    if result['status'] == 'success' and result['changes']:
      changes = result['changes']
      unreviewed = sum(1 for c in changes if not c.get('acknowledged_at'))
      total = len(changes)

      self.lbl_summary.text = (
        f"{total} change{'s' if total != 1 else ''}  |  "
        f"{unreviewed} unreviewed"
      )

      lines = []
      for c in changes:
        detected = c.get('detected_at', '')
        try:
          from datetime import datetime as dt
          d = dt.fromisoformat(detected)
          detected = d.strftime('%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          pass

        ack = c.get('acknowledged_at')
        ack_marker = '' if ack else ' *(unreviewed)*'

        if c['change_type'] == 'FIELD_CHANGE':
          field = c.get('field_name', 'unknown')
          old = c.get('old_value', '') or '(empty)'
          new = c.get('new_value', '') or '(empty)'
          lines.append(
            f"**{detected}** — **{field}** changed{ack_marker}\n\n"
            f"&nbsp;&nbsp;&nbsp;&nbsp;**Before:**&nbsp;&nbsp;{old}\n\n"
            f"&nbsp;&nbsp;&nbsp;&nbsp;**After:**&nbsp;&nbsp;&nbsp;&nbsp;{new}"
          )
        else:
          old_status = c.get('old_value', '') or ''
          new_status = c.get('new_value', '') or ''
          lines.append(
            f"**{detected}** — **{c['change_type']}**{ack_marker}\n\n"
            f"&nbsp;&nbsp;&nbsp;&nbsp;**Before:**&nbsp;&nbsp;{old_status}\n\n"
            f"&nbsp;&nbsp;&nbsp;&nbsp;**After:**&nbsp;&nbsp;&nbsp;&nbsp;{new_status}"
          )

      self.rt_changes.content = '\n\n'.join(lines)

      # Show acknowledge button only if there are unreviewed changes
      # and the WO is not already Complete
      self.btn_acknowledge.visible = (
        unreviewed > 0 and item.get('status') != 'Complete'
      )
      self.btn_acknowledge.tag = item['work_order_id']
    else:
      self.lbl_summary.text = "No changes detected"
      self.rt_changes.content = ""
      self.btn_acknowledge.visible = False

  @handle("btn_acknowledge", "click")
  def btn_acknowledge_click(self, **event_args):
    """Acknowledge all pending changes for this work order."""
    work_order_id = self.btn_acknowledge.tag
    if work_order_id:
      result = anvil.server.call(
        'acknowledge_work_order_changes',
        work_order_id
      )
      if result['status'] == 'success':
        Notification("Changes acknowledged").show()
        self.btn_acknowledge.visible = False
        self.lbl_summary.text = "All changes acknowledged"
        # Signal the parent to refresh the grid
        self.raise_event('x-close')
        # PC request 2026-10-20: close the popup on acknowledge instead of
        # making the user click Close as a second step. 'x-close-alert' is
        # Anvil's built-in: it dismisses the alert() this form is sitting
        # in; the parent's x-close handler above has already flagged the
        # grid refresh, which runs when alert() returns.
        self.raise_event('x-close-alert', value=True)
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")

