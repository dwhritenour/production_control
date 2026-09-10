from ._anvil_designer import WorkOrdersRowTemplateTemplate
from anvil import *
import anvil.server



class WorkOrdersRowTemplate(WorkOrdersRowTemplateTemplate):
  """
    Office (Production) work-order row -- READ + INLINE EDIT only.

    2026-07-27: the Actions column (Release / Cancel / Print buttons) was
    REMOVED. All work-order actions live in the WorkOrderDetail popup
    (click the WO#), which is Routing-v2-aware. The grid buttons were
    redundant AND dangerous: the grid's Release still used the pre-routing
    direct status flip, bypassing the routing editor entirely.

    What remains here: status color label, change badge, due-date display,
    inline WO-due-date + override-qty edits, notes preview, and the
    WO# / SO# popup links.
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

    # ---- SO line description (061) ------------------------------------------
    # The Description column is code-added in the parent (auto cell shows
    # sku_desc). A Label named lbl_desc placed in that column takes over;
    # formatting stays the designer's (Dave, 09-10: no bold).
    if hasattr(self, 'lbl_desc'):
      self.lbl_desc.text = self.item.get('sku_desc') or ''

    # ---- Daily Plan stamp (056, 2026-09-09) ---------------------------------
    # Same cue as the condensed grid: a work order on TODAY'S plan gets a
    # green row and an arrow on the WO#; an older-day stamp (planned, never
    # moved, not on today's plan) gets a pale amber row. If a Link/Label
    # named lbl_plan sits in the Plan column it shows 'M-3' with tier colour
    # and opens the Daily Plan app; otherwise the code-added column's auto
    # cell shows plan_label on its own.
    if hasattr(self, 'lbl_plan'):
      self.lbl_plan.text = self.item.get('plan_label') or ''
      tier = (self.item.get('plan_tier') or '')
      self.lbl_plan.bold = bool(self.item.get('on_todays_plan'))
      self.lbl_plan.foreground = {'Must': '#C62828', 'Next': '#EF6C00',
                                  'Stretch': '#78909C'}.get(tier, '#455A64')
      if self.item.get('plan_label'):
        self.lbl_plan.tooltip = (f"{'Today' if self.item.get('on_todays_plan') else 'Planned'}"
                                 f" {self.item.get('plan_label')} - open the Daily Plan")
        try:
          self.lbl_plan.set_event_handler('click', self._open_daily_plan)
        except Exception:
          pass                   # a Label has no click event; a Link does
    try:
      if self.item.get('on_todays_plan'):
        self.background = '#E8F5E9'
        self.link_wo_number.text = '\u25b6 ' + (self.item.get('work_order_number') or '')
        self.link_wo_number.foreground = '#1B5E20'
        self.link_wo_number.tooltip = f"On today's plan: {self.item.get('plan_label') or ''}"
      elif self.item.get('plan_date'):
        self.background = '#FFF8E1'
        self.link_wo_number.tooltip = f"Planned {self.item.get('plan_label') or ''} - not on today's plan"
      else:
        self.background = ''
    except Exception:
      pass

  DAILY_PLAN_URL = 'https://titan-plan.anvil.app'

  def _open_daily_plan(self, **event_args):
    """Open the Titan Daily Plan app in a new browser tab."""
    try:
      import anvil.js
      anvil.js.window.open(self.DAILY_PLAN_URL, '_blank')
    except Exception:
      pass                     # nothing sensible to do in-app; stay put

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