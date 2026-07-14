from ._anvil_designer import WorkOrdersRowTemplateTemplate
from anvil import *
import anvil.server
import anvil.media


# Statuses from which the office may still cancel a work order.
# (Cancelled / Complete / the three Verify states are terminal for the office.)
CANCELLABLE_STATUSES = ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')


class WorkOrdersRowTemplate(WorkOrdersRowTemplateTemplate):
  """
    Office (Production) work-order row.

    SE Department rollout (2026-06-17): the office no longer Starts or Completes
    work orders -- that moved to the SE Department app. The office now:
       Release  (Created -> Released)  -- makes the WO visible on the floor
       Cancel   (-> Cancelled)         -- terminal; allowed from any live status
       Print    (download traveler)    -- unchanged

    The two existing designer buttons are REPURPOSED in code (captions + handlers)
    so no designer rebuild is required for the test rollout:
       btn_release    -> "Release"
       btn_cancel -> "Cancel"
    (Rename the components in the designer later for clarity.)
  """

  def __init__(self, **properties):
    self.init_components(**properties)

    status = self.item.get('status', '')

    # ---- Status color coding via explicit label --------------------------
    self.lbl_status.text = status
    if status == 'Created':
      self.lbl_status.foreground = '#2196F3'  # blue
      self.lbl_status.bold = True
    elif status == 'Released':
      self.lbl_status.foreground = '#00897B'  # teal -- handed to the floor
      self.lbl_status.bold = True
    elif status == 'Assigned':
      self.lbl_status.foreground = '#607D8B'  # blue-gray
      self.lbl_status.bold = True
    elif status == 'In Progress':
      self.lbl_status.foreground = '#FF9800'  # amber
      self.lbl_status.bold = True
    elif status == 'Paused':
      self.lbl_status.foreground = '#C62828'  # red-ish -- stalled on the floor
      self.lbl_status.bold = True
    elif status == 'Complete':
      self.lbl_status.foreground = '#4CAF50'  # green
      self.lbl_status.bold = True
    elif status in ('Shipped - Verify', 'Invoiced - Verify',
                    'Closed - Verify', 'Cancelled'):
      self.lbl_status.foreground = '#7B2FBE'  # purple
      self.lbl_status.bold = True

    # ---- Change badge -- clickable to open change history popup -----------
    change_count = self.item.get('pending_change_count', 0) or 0
    if change_count > 0 and status != 'Complete':
      self.lbl_change_badge.text = f"{change_count} change{'s' if change_count != 1 else ''}"
      self.lbl_change_badge.visible = True
      self.lbl_change_badge.set_event_handler('click', self.show_change_history)
    else:
      self.lbl_change_badge.visible = False

    # ---- Action buttons (office: Release / UnRelease / Cancel / Print) ---
    # btn_release is dual-purpose -- the two states are mutually exclusive,
    # so no new designer component is required:
    #   Created  -> "Release"   (Created  -> Released)
    #   Released -> "UnRelease" (Released -> Created)   [added 2026-06-23]
    if status == 'Created':
      self.btn_release.text = 'Release'
      self.btn_release.visible = True
    elif status == 'Released':
      self.btn_release.text = 'UnRelease'
      self.btn_release.visible = True
    else:
      self.btn_release.visible = False
    # btn_cancel repurposed as CANCEL
    self.btn_cancel.text = 'Cancel'
    self.btn_cancel.visible = (status in CANCELLABLE_STATUSES)
    # Print stays available for any work order
    self.btn_print.visible = True

    # ---- Due date display with overdue highlighting ----------------------
    due_str = self.item.get('due_date', '')
    self.lbl_due_date.text = due_str or ''
    if due_str and status != 'Complete':
      try:
        from datetime import datetime, date
        due = datetime.strptime(due_str, '%m/%d/%Y').date()
        if due < date.today():
          self.lbl_due_date.foreground = '#F44336'  # Red
          self.lbl_due_date.bold = True
      except (ValueError, TypeError):
        pass

    # ---- WO Due Date -- inline DatePicker for production manager ----------
    wo_due_str = self.item.get('wo_due_date', '')
    if wo_due_str:
      try:
        from datetime import datetime as dt
        self.dp_wo_due_date.date = dt.strptime(wo_due_str, '%m/%d/%Y').date()
      except (ValueError, TypeError):
        try:
          self.dp_wo_due_date.date = dt.fromisoformat(wo_due_str).date()
        except (ValueError, TypeError):
          self.dp_wo_due_date.date = None
    else:
      self.dp_wo_due_date.date = None

    # Disable WO due date editing on completed WOs
    self.dp_wo_due_date.enabled = (status != 'Complete')

    # Highlight WO due date if overdue and not complete
    if self.dp_wo_due_date.date and status != 'Complete':
      from datetime import date as date_type
      if self.dp_wo_due_date.date < date_type.today():
        self.dp_wo_due_date.foreground = '#F44336'  # Red
        self.dp_wo_due_date.bold = True

    # ---- Override qty -----------------------------------------------------
    override = self.item.get('wo_override_qty')
    self.tb_override_qty.text = str(int(override)) if override is not None else ''
    self.tb_override_qty.enabled = (status != 'Complete')

    # ---- Notes -- click to see full text ---------------------------------
    notes = self.item.get('notes', '') or ''
    if len(notes) > 40:
      self.link_notes.text = notes[:40] + '...'
    else:
      self.link_notes.text = notes
    if notes:
      self.link_notes.set_event_handler('click', lambda **e: alert(notes, title="Notes"))

    # ---- WO# / SO# links -------------------------------------------------
    self.link_wo_number.text = self.item.get('work_order_number', '')
    self.link_wo_number.bold = True
    self.link_wo_number.set_event_handler('click', self.show_detail)

    self.link_so_number.text = self.item.get('so_number_display', '')
    self.link_so_number.set_event_handler('click', self.show_backorder_detail)

  # =========================================================================
  # RELEASE / UNRELEASE  (btn_release, dual-purpose)
  #   Created  -> Released   (Release)
  #   Released -> Created    (UnRelease)
  # =========================================================================
  @handle("btn_release", "click")
  def btn_release_click(self, **event_args):
    status = self.item.get('status', '')
    wo_num = self.item.get('work_order_number', '')

    if status == 'Released':
      # UnRelease: pull the WO back off the SE Department board.
      if not confirm(
        f"Un-release WO {wo_num} back to 'Created'?\n\n"
        "It will be removed from the SE Department board until it is "
        "released again."
      ):
        return
      result = anvil.server.call('unrelease_work_order',
                                 self.item['work_order_id'])
      ok_msg = f"WO {wo_num} un-released (back to Created)"
    else:
      # Release: hand the WO to the floor.
      result = anvil.server.call('update_work_order_status',
                                 self.item['work_order_id'], 'Released')
      ok_msg = f"WO {wo_num} released to the SE Department"

    if result['status'] == 'success':
      Notification(ok_msg).show()
      get_open_form().refresh_data()
    else:
      alert(f"Error: {result.get('message', 'Unknown error')}")

  # =========================================================================
  # CANCEL  (btn_cancel)  any live status -> Cancelled
  # =========================================================================
  @handle("btn_cancel", "click")
  def btn_cancel_click(self, **event_args):
    status = self.item.get('status', '')
    wo_num = self.item.get('work_order_number', '')

    if status in ('In Progress', 'Paused'):
      # The floor has already started this job -- warn explicitly.
      msg = (f"WO {wo_num} is currently {status} on the floor.\n\n"
             "Cancelling will remove it from the SE Department board "
             "immediately. Continue?")
    else:
      msg = f"Cancel WO {wo_num}? This cannot be undone."

    if not confirm(msg):
      return

    result = anvil.server.call(
      'update_work_order_status',
      self.item['work_order_id'],
      'Cancelled'
    )
    if result['status'] == 'success':
      Notification(f"WO {wo_num} cancelled").show()
      get_open_form().refresh_data()
    else:
      alert(f"Error: {result.get('message', 'Unknown error')}")

  # =========================================================================
  # PRINT
  # =========================================================================
  @handle("btn_print", "click")
  def btn_print_click(self, **event_args):
    """Generate and download work order PDF, then stamp printed_at."""
    result = anvil.server.call(
      'download_work_order_pdf',
      self.item['work_order_id']
    )
    if result['status'] == 'success':
      anvil.media.download(result['pdf_media'])
      # Best-effort printed stamp (don't block the download on it).
      try:
        anvil.server.call('mark_work_order_printed', self.item['work_order_id'])
      except Exception:
        pass
      Notification("Work order PDF downloaded").show()
    else:
      alert(f"Error: {result.get('message', 'Unknown error')}")

  # =========================================================================
  # POPUPS
  # =========================================================================
  def show_backorder_detail(self, **event_args):
    """Open sales backorder detail as a popup dialog."""
    from ..SalesBackorderDetail import SalesBackorderDetail
    so_num = self.item.get('sales_order_number', '')
    detail_form = SalesBackorderDetail(sales_order_number=so_num)
    alert(content=detail_form, title="", large=True, buttons=[("Close", None)])

  def show_detail(self, **event_args):
    """Open work order detail as a popup dialog."""
    from ..WorkOrderDetail import WorkOrderDetail
    detail_form = WorkOrderDetail(item=self.item)
    needs_refresh = [False]
    def on_close(**e):
      needs_refresh[0] = True
    detail_form.add_event_handler('x-close', on_close)
    alert(content=detail_form, title="", large=True, buttons=[("Close", None)])
    if needs_refresh[0]:
      get_open_form().refresh_data()

  def show_change_history(self, **event_args):
    """Open change history as a popup dialog."""
    from ..ChangeHistoryDetail import ChangeHistoryDetail
    detail_form = ChangeHistoryDetail(item=self.item)
    needs_refresh = [False]
    def on_close(**e):
      needs_refresh[0] = True
    detail_form.add_event_handler('x-close', on_close)
    alert(content=detail_form, title="", large=True, buttons=[("Close", None)])
    if needs_refresh[0]:
      get_open_form().refresh_data()

  # =========================================================================
  # INLINE EDITS (WO due date / override qty)
  # =========================================================================
  @handle("dp_wo_due_date", "change")
  def dp_wo_due_date_change(self, **event_args):
    """Save WO due date when production manager selects a date."""
    new_date = self.dp_wo_due_date.date
    date_str = new_date.isoformat() if new_date else None
    result = anvil.server.call(
      'update_work_order_due_date',
      self.item['work_order_id'],
      date_str
    )
    if result['status'] == 'success':
      self.item['wo_due_date'] = new_date.strftime('%m/%d/%Y') if new_date else ''
      Notification("WO due date saved").show()
    else:
      alert(f"Error saving WO due date: {result.get('message', 'Unknown error')}")

  @handle("tb_override_qty", "lost_focus")
  def tb_override_qty_lost_focus(self, **event_args):
    raw = self.tb_override_qty.text
    try:
      override_qty = int(raw) if raw and raw.strip() != '' else None
    except ValueError:
      alert("Override quantity must be a whole number.")
      return
    result = anvil.server.call(
      'update_work_order_override_qty',
      self.item['work_order_id'],
      override_qty
    )
    if result['status'] == 'success':
      self.item['wo_override_qty'] = override_qty
    else:
      alert(f"Error saving override: {result.get('message', 'Unknown error')}")

  @handle("link_so_number", "click")
  def link_so_number_click(self, **event_args):
    pass