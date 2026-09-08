from ._anvil_designer import CondensedRowTemplateTemplate
from anvil import *
import anvil.server


class CondensedRowTemplate(CondensedRowTemplateTemplate):
  """
    Row for the condensed Active Work Orders grid.

    Components (placed by DataGrid column):
      link_wo_number   -> WO#    (click opens WorkOrderDetail = all actions+notes)
      flow_status      -> Status (lbl_status + lbl_change_badge)
      link_so_number   -> SO#
      tb_override_qty  -> Override Qty (editable)
      dp_wo_due_date   -> WO Due Date (editable)
      lbl_due_date     -> SO Due Date
    data_key columns (no component): Part# (part_number), Material / SE
      (material_se), Qty (quantity).
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    status = self.item.get('status', '')

    # ---- Status color (matches the full work_orders grid) ----------------
    self.lbl_status.text = status
    if status == 'Created':
      self.lbl_status.foreground = '#2196F3'
      self.lbl_status.bold = True
    elif status == 'Released':
      self.lbl_status.foreground = '#00897B'
      self.lbl_status.bold = True
    elif status == 'Assigned':
      self.lbl_status.foreground = '#607D8B'  # blue-gray
      self.lbl_status.bold = True
    elif status == 'In Progress':
      self.lbl_status.foreground = '#FF9800'  # amber
      self.lbl_status.bold = True
    elif status == 'Paused':
      self.lbl_status.foreground = '#C62828'  # red -- stalled on the floor
      self.lbl_status.bold = True
    else:
      self.lbl_status.foreground = '#455A64'
      self.lbl_status.bold = True

    # ---- Change badge (opens change history) -----------------------------
    change_count = self.item.get('pending_change_count', 0) or 0
    if change_count > 0 and status != 'Complete':
      self.lbl_change_badge.text = f"{change_count} change{'s' if change_count != 1 else ''}"
      self.lbl_change_badge.visible = True
      self.lbl_change_badge.set_event_handler('click', self.show_change_history)
    else:
      self.lbl_change_badge.visible = False

    # ---- Part # / Material (stacked in one column, same formatting) ------
    self.lbl_part.text = self.item.get('part_number', '')
    self.lbl_material.text = self.item.get('material_se', '')

    # ---- Qty (Label so it can be formatted in the designer) --------------
    if hasattr(self, 'lbl_qty'):
      qty = self.item.get('quantity')
      self.lbl_qty.text = str(int(qty)) if qty is not None else ''

    # ---- WO# link -> detail popup (carries all actions + notes) -----------
    self.link_wo_number.text = self.item.get('work_order_number', '')
    self.link_wo_number.bold = True
    self.link_wo_number.set_event_handler('click', self.show_detail)

    # ---- SO# link -> backorder detail (rush ⚡ cue mirrors sales_orders_due) -
    so_text = self.item.get('so_number_display', '')
    if self.item.get('is_rush'):
      rush_glyph = ' ⚡'
      if not so_text.endswith(rush_glyph):
        so_text += rush_glyph
      carr = (self.item.get('nx_carrier') or '').strip()
      mos = (self.item.get('nx_mos') or '').strip()
      label = ' '.join(p for p in (carr, mos) if p)
      self.link_so_number.tooltip = f"Rush shipment — {label}" if label else "Rush shipment"
    self.link_so_number.text = so_text
    self.link_so_number.set_event_handler('click', self.show_backorder_detail)

    # ---- SO Due Date with overdue highlight ------------------------------
    due_str = self.item.get('due_date', '')
    self.lbl_due_date.text = due_str or ''
    if due_str and status != 'Complete':
      try:
        from datetime import datetime, date
        if datetime.strptime(due_str, '%m/%d/%Y').date() < date.today():
          self.lbl_due_date.foreground = '#F44336'
          self.lbl_due_date.bold = True
      except (ValueError, TypeError):
        pass

    # ---- WO Due Date inline editor ---------------------------------------
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
    if self.dp_wo_due_date.date:
      from datetime import date as date_type
      if self.dp_wo_due_date.date < date_type.today():
        self.dp_wo_due_date.foreground = '#F44336'
        self.dp_wo_due_date.bold = True

    # ---- Override qty -----------------------------------------------------
    override = self.item.get('wo_override_qty')
    self.tb_override_qty.text = str(int(override)) if override is not None else ''

    # ---- Daily Plan stamp (056) ---------------------------------------------
    # The 'Plan' column is code-added in the parent (auto cell shows
    # plan_label). If you later drop a Label named lbl_plan into that column,
    # it takes over with tier colouring: Must red, Next amber, Stretch grey.
    if hasattr(self, 'lbl_plan'):
      self.lbl_plan.text = self.item.get('plan_label') or ''
      tier = (self.item.get('plan_tier') or '')
      self.lbl_plan.bold = bool(self.item.get('on_todays_plan'))
      self.lbl_plan.foreground = {'Must': '#C62828', 'Next': '#EF6C00',
                                  'Stretch': '#78909C'}.get(tier, '#455A64')
    # Row cue (Dave, 2026-09-08): a work order on TODAY'S plan is unmistakable
    # at a glance - green-tinted row, WO# in plan green with a marker. A stamp
    # from an OLDER day (planned, never moved, not on today's plan) gets a
    # pale amber row so it is not mistaken for today's work.
    try:
      if self.item.get('on_todays_plan'):
        self.background = '#E8F5E9'
        self.link_wo_number.text = '▶ ' + (self.item.get('work_order_number') or '')
        self.link_wo_number.foreground = '#1B5E20'
        self.link_wo_number.tooltip = f"On today's plan: {self.item.get('plan_label') or ''}"
      elif self.item.get('plan_date'):
        self.background = '#FFF8E1'
        self.link_wo_number.tooltip = f"Planned {self.item.get('plan_label') or ''} - not on today's plan"
      else:
        self.background = ''
    except Exception:
      pass

  # =========================================================================
  # POPUPS  (reuse the existing WorkOrderDetail / detail forms)
  # =========================================================================
  def show_detail(self, **event_args):
    from ...work_orders.WorkOrderDetail import WorkOrderDetail
    detail_form = WorkOrderDetail(item=self.item)
    needs_refresh = [False]
    detail_form.add_event_handler('x-close', lambda **e: needs_refresh.__setitem__(0, True))
    alert(content=detail_form, title="", large=True, buttons=[("Close", None)])
    if needs_refresh[0]:
      get_open_form().refresh_data()

  def show_backorder_detail(self, **event_args):
    from ...work_orders.SalesBackorderDetail import SalesBackorderDetail
    so_num = self.item.get('sales_order_number', '')
    alert(content=SalesBackorderDetail(sales_order_number=so_num),
          title="", large=True, buttons=[("Close", None)])

  def show_change_history(self, **event_args):
    from ...work_orders.ChangeHistoryDetail import ChangeHistoryDetail
    detail_form = ChangeHistoryDetail(item=self.item)
    needs_refresh = [False]
    detail_form.add_event_handler('x-close', lambda **e: needs_refresh.__setitem__(0, True))
    alert(content=detail_form, title="", large=True, buttons=[("Close", None)])
    if needs_refresh[0]:
      get_open_form().refresh_data()

  # =========================================================================
  # INLINE EDITS
  # =========================================================================
  @handle("dp_wo_due_date", "change")
  def dp_wo_due_date_change(self, **event_args):
    new_date = self.dp_wo_due_date.date
    result = anvil.server.call('update_work_order_due_date',
                               self.item['work_order_id'],
                               new_date.isoformat() if new_date else None)
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
    result = anvil.server.call('update_work_order_override_qty',
                               self.item['work_order_id'], override_qty)
    if result['status'] == 'success':
      self.item['wo_override_qty'] = override_qty
    else:
      alert(f"Error saving override: {result.get('message', 'Unknown error')}")