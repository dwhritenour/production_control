from ._anvil_designer import WorkOrdersRowTemplateTemplate
from anvil import *
import anvil.server


def _release_summary(res):
  """'F>P>A - Material Handling, Pre-Assembly auto-completed - now Ready: Assemble'
  from a release_work_order result."""
  bits = [res.get('route_summary') or '']
  auto = [a.get('department_name') for a in (res.get('auto_completed') or [])]
  if auto:
    bits.append(', '.join(auto) + ' auto-completed (no board yet)')
  nxt = [n.get('department_name') + ('' if n.get('texted') else ' (no text)')
         for n in (res.get('next_ready') or [])]
  if nxt:
    bits.append('now Ready: ' + ', '.join(nxt))
  elif not auto:
    first = [f.get('department_name') for f in (res.get('first_steps') or [])]
    if first:
      bits.append('step 1 Ready at ' + ', '.join(first))
  return ' - '.join(b for b in bits if b)



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

    2026-10-14 (default routing, migration 108): every order arrives routed,
    so a guarded one-click release is back on the row - the Quick Release
    cell (cb_pick + btn_quick_release) calls routing_desk.release_work_order,
    which flips step 1 Pending -> Ready, texts if anyone subscribes, sets
    Released and logs; it never rewrites the routing (the old grid button
    did a bare status flip; this one goes through the same guarded path as
    the v2.0 Desk). Part# / Description and Material / SE are stacked cells
    (cp_part, cp_config) to make room for it.
  """

  def __init__(self, **properties):
    self.init_components(**properties)

    status = self.item.get('status', '')
    self._paint_status(status)

    # ---- Stacked cells (2026-10-14): Part# over Description, Material over SE
    self.lbl_part.text = self.item.get('part_number') or ''
    self.lbl_material.text = (self.item.get('material_value') or '').strip()
    self.lbl_se.text = (self.item.get('se_value') or '').strip()
    if self.item.get('is_nonstock'):
      self.lbl_part.italic = True

    # ---- Quick Release cell (2026-10-14) -------------------------------------
    self._wire_quick_release(status)

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

    # ---- Notes -- preview in the grid, full text fetched on click ---------
    # 2026-09-24 (performance): the grid no longer carries the notes text
    # (three varchar(max) columns x thousands of rows = most of a 4 MB
    # payload). The server sends has_notes + a 60-char notes_preview; the
    # click calls get_wo_notes for the full SKU / SO notes. Falls back to
    # the old inline 'notes' key if an older server module is deployed.
    preview = (self.item.get('notes_preview') or self.item.get('notes') or '').strip()
    has_notes = bool(self.item.get('has_notes')) or bool(preview)
    if len(preview) > 40:
      self.link_notes.text = preview[:40] + '...'
    else:
      self.link_notes.text = preview or ('Notes' if has_notes else '')
    if has_notes:
      self.link_notes.set_event_handler('click', self.show_notes)

    # ---- WO# / SO# links -------------------------------------------------
    self.link_wo_number.text = self.item.get('work_order_number', '')
    self.link_wo_number.bold = True
    self.link_wo_number.set_event_handler('click', self.show_detail)

    self.link_so_number.text = self.item.get('so_number_display', '')
    self.link_so_number.set_event_handler('click', self.show_backorder_detail)

    # ---- Routing diagram link (2026-09-12) -----------------------------------
    # 'View' opens the swim-lane routing diagram for this WO in a popup
    # (routing_diagram.show_routing_popup, reads get_wo_routing). If the
    # designer placed a Link named lk_routing in the Routing column it is
    # used; otherwise one is added in code to the column the parent form
    # created (work_orders._add_routing_column).
    self._wire_routing_link(status)

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

  def _paint_status(self, status):
    """Status colour coding via explicit label (also re-run after a Quick
    Release flips the row to Released)."""
    self.lbl_status.text = status
    self.lbl_status.foreground = ''
    self.lbl_status.bold = False
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


  # =========================================================================
  # QUICK RELEASE (2026-10-14) - the guarded one-click release on the row
  # =========================================================================
  def _wire_quick_release(self, status):
    parent = None
    try:
      parent = get_open_form()
    except Exception:
      pass
    picked = False
    try:
      picked = self.item.get('work_order_id') in getattr(parent, '_picked', set())
    except Exception:
      picked = False
    self.cb_pick.checked = picked
    releasable = (status == 'Created')
    self.cb_pick.visible = releasable
    self.btn_quick_release.visible = releasable
    self.cb_pick.set_event_handler('change', self._pick_change)
    self.btn_quick_release.set_event_handler('click', self._quick_release_click)

  def _pick_change(self, **event_args):
    try:
      get_open_form().toggle_pick(self.item.get('work_order_id'), self.cb_pick.checked)
    except Exception:
      pass

  def _quick_release_click(self, **event_args):
    wid = self.item.get('work_order_id')
    won = self.item.get('work_order_number', '')
    self.btn_quick_release.enabled = False
    self.btn_quick_release.text = '...'
    try:
      res = anvil.server.call('release_work_order', wid, None, 'quick')
    finally:
      self.btn_quick_release.enabled = True
      self.btn_quick_release.text = 'Release'
    if res.get('status') != 'success':
      alert(f"{won}: {res.get('message', 'Unknown error')}")
      return
    self.item['status'] = 'Released'
    self._paint_status('Released')
    self.cb_pick.visible = False
    self.btn_quick_release.visible = False
    try:
      get_open_form().toggle_pick(wid, False)
      get_open_form().note_released(1)
    except Exception:
      pass
    Notification(f"{won} released - {_release_summary(res)}").show()

  # Statuses that always carry a saved routing (release writes it). A
  # 'Created' WO may still have one (UnRelease keeps the snapshot), so the
  # link shows for every row; the popup explains when nothing is saved.
  ROUTED_STATUSES = ('Released', 'Assigned', 'In Progress', 'Paused', 'Complete')

  def _wire_routing_link(self, status):
    link = getattr(self, 'lk_routing', None)
    if link is None:
      col_id = None
      try:
        parent = get_open_form()
        if hasattr(parent, 'routing_column_id'):
          col_id = parent.routing_column_id()
      except Exception:
        col_id = None
      if not col_id:
        return                      # no Routing column on this grid
      link = Link(text='View')
      try:
        self.add_component(link, column=col_id)
      except Exception:
        return
    link.text = 'View'
    link.tooltip = 'Show the routing diagram for this work order'
    if status in self.ROUTED_STATUSES:
      link.bold = True
    else:
      link.foreground = '#78909C'   # muted: may not be routed yet
    try:
      link.set_event_handler('click', self.show_routing)
    except Exception:
      pass

  def show_routing(self, **event_args):
    """Popup: swim-lane routing diagram for this work order."""
    from ... import routing_diagram
    routing_diagram.show_routing_popup(
      self.item.get('work_order_id'),
      self.item.get('work_order_number', ''),
      self.item.get('part_number', ''))

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
  def show_notes(self, **event_args):
    """Full SKU + sales-order notes for this WO (one small call)."""
    result = anvil.server.call('get_wo_notes', self.item['work_order_id'])
    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return
    panel = ColumnPanel()
    sku = (result.get('so_notes') or result.get('notes') or '').strip()
    so = (result.get('so_level_notes') or '').strip()
    if sku:
      panel.add_component(Label(text='SKU Notes', bold=True))
      panel.add_component(Label(text=sku, foreground='#333333'))
    if so:
      panel.add_component(Label(text='Sales Order Notes', bold=True))
      panel.add_component(Label(text=so, foreground='#333333'))
    if not sku and not so:
      panel.add_component(Label(text='(no notes)', italic=True))
    alert(content=panel, title=f"Notes - {self.item.get('work_order_number', '')}",
          large=True, buttons=[("Close", None)])

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