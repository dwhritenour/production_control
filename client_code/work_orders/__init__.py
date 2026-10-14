from ._anvil_designer import work_ordersTemplate
from anvil import *
import anvil.server


# Daily Plan (056, 2026-09-09): same treatment as work_orders_condensed.
# Chip accent = plan green; the chip is the optional Link lk_plan_count.
PLAN_CARD_COLOR = '#1B5E20'

# Theme role for the count / Rush / Plan chips (Dave, 2026-09-09): the Link's
# built-in 8px padding cannot be changed in the designer, so a custom role
# 'count-chip' in the App Theme carries padding + radius. Code applies it here
# so both work-order forms stay identical. Blank it to fall back to plain Links.
CHIP_ROLE = 'count-chip'


SORT_MAP = {
  'Plan (position)': 'plan_sort',
  'Due Date (Earliest)': 'due_date',
  'SO# (Ascending)': 'sales_order_number',
  'WO# (Ascending)': 'work_order_number',
  'Part# (Ascending)': 'part_number',
  'Created Date (Newest)': 'created_at',
}


class work_orders(work_ordersTemplate):
  def __init__(self, search_text=None, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.grid_work_orders.role = ['tonal-data-grid', 'horizontal-scroll']
    self.nav.active = 'work_orders'

    # Sort state
    self._sort_key = None
    self._sort_reverse = False
    self._work_orders = []

    # Count row acts like buttons (2026-09-09, from work_orders_condensed)
    self._wire_count_filters()

    # GridPager custom component under the grid (optional - see
    # documents/GridPager_Component_Specification.md). Without it the
    # DataGrid's own paging applies, as before.
    _pg = self._find_pager()
    if _pg is not None:
      try:
        _pg.attach(self.grid_work_orders, self.repeating_panel_1)
      except Exception:
        pass

    # Daily Plan (056): 'Plan' column, chip, plan-only filter, sort option
    self._plan_only = False
    self._add_plan_column()
    # 2026-10-14: Description now stacks under Part# in the row template
    # (cp_part), so the code-added Description column is no longer created.
    self._add_routing_column()

    # Quick Release (2026-10-14): the checked set + the batch button. Rows
    # keep their checkbox in step with _picked across pages / re-renders.
    self._picked = set()
    self._add_quick_release_button()
    self._wire_plan_filter()
    try:
      if 'Plan (position)' not in (self.dd_sort.items or []):
        self.dd_sort.items = ['Plan (position)'] + list(self.dd_sort.items or [])
    except Exception:
      pass

    # Hide the old inline detail panel (kept in YAML for rollback, hidden permanently)
    if hasattr(self, 'panel_detail'):          # old inline panel; deleted 09-09
      self.panel_detail.visible = False

    # Pre-filter from a deep link (e.g. WO link on Sales Orders Due form)
    if search_text:
      self.tb_search.text = search_text

    # Volume guard (2026-08-31): default the status filter to 'Created' so
    # the initial load stays well under the payload size that broke after
    # the all-WO expansion (Dave's pick over an Active-only default).
    self.dd_status_filter.selected_value = 'Created'

    # Load initial data
    self.refresh_data()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def refresh_data(self):
    """Fetch work orders from server and populate grid."""
    # Get filter values
    status_filter = self.dd_status_filter.selected_value
    if status_filter == 'All' or status_filter is None:
      status_filter = None

    search_text = self.tb_search.text or ''
    if not search_text.strip():
      search_text = None

    # Call server
    result = anvil.server.call(
      'get_work_orders',
      status_filter=status_filter,
      search_text=search_text,
      active_only=self.cb_active_only.checked,
      backorder_only=self.cb_backorder_only.checked,
    )

    if result['status'] == 'success':
      self._work_orders = result['work_orders']
      self._picked = set()
      self._style_quick_release_button()
      if self._plan_only:
        # 056: only today's published plan (server already sorted it first)
        self._work_orders = [w for w in self._work_orders if w.get('on_todays_plan')]

      # Re-apply active sort if one is selected
      self._reapply_sort()

      self._show_rows()

      # Update record count
      count = len(result['work_orders'])
      total_qty = sum(wo.get('quantity', 0) or 0 for wo in result['work_orders'])
      self.lbl_record_count.text = f"{count} work orders  |  {total_qty:,} straining elements"
      if result.get('truncated'):
        self.lbl_record_count.text += f"  (showing first {count:,} - refine filters)"

      # Update summary cards
      counts = result['counts']
      self.lbl_created_count.text = f"Created: {counts.get('Created', 0)}"
      self.lbl_in_progress_count.text = f"In Progress: {counts.get('In Progress', 0)}"
      self.lbl_complete_count.text = f"Complete: {counts.get('Complete', 0)}"

      self.lbl_changes_count.text = f"Changes: {counts.get('PendingChanges', 0)}"
      if hasattr(self, 'lk_plan_count'):
        self.lk_plan_count.text = (f"Plan: {counts.get('plan_released', 0)}/"
                                   f"{counts.get('plan_today', 0)} released")
      self._style_plan_link()
      self._style_count_links()

      # Update last refreshed
      from datetime import datetime
      now = datetime.now().strftime('%I:%M %p')
      self.lbl_last_refreshed.text = f"Last Refreshed: {now}"
    else:
      alert(f"Error loading work orders: {result.get('message', 'Unknown error')}")

  # =========================================================================
  # Clear Filters
  # =========================================================================
  @handle("btn_clear_filters", "click")
  def btn_clear_filters_click(self, **event_args):
    self.dd_status_filter.selected_value = None
    self.dd_sort.selected_value = None
    self.tb_search.text = ''
    self.cb_active_only.checked = False
    self.cb_backorder_only.checked = False
    self._plan_only = False
    self.refresh_data()

  # =========================================================================
  # ROWS -> GRID  (through the GridPager component when one is present)
  # =========================================================================
  def _show_rows(self):
    _pg = self._find_pager()
    if _pg is not None:
      try:
        _pg.items = self._work_orders
        return
      except Exception:
        pass
    self.repeating_panel_1.items = self._work_orders

  # =========================================================================
  # GRID PAGER  (custom component grid_pager, any instance name)
  # =========================================================================
  def _find_pager(self):
    """The grid_pager component dropped under the grid, whatever it was
    named in the designer (pager, grid_pager_1, ...). Duck-typed: anything
    with attach() and page_items. None when the form has no pager."""
    if getattr(self, '_pager_cache', None) is not None:
      return self._pager_cache
    for name in ('pager', 'grid_pager_1', 'grid_pager'):
      c = getattr(self, name, None)
      if c is not None and hasattr(c, 'attach') and hasattr(c, 'page_items'):
        self._pager_cache = c
        return c
    def walk(container):
      try:
        kids = container.get_components()
      except Exception:
        return None
      for k in kids:
        if hasattr(k, 'attach') and hasattr(k, 'page_items'):
          return k
        found = walk(k)
        if found is not None:
          return found
      return None
    self._pager_cache = walk(self)
    return self._pager_cache

  # =========================================================================
  # CLICKABLE COUNT FILTERS  (click a count to apply that filter)
  # =========================================================================
  # attr -> (filter value, color). Colors match the work-order ROW status
  # colors so the whole app is consistent. Set in code so they never need to
  # be picked by hand in the designer. Make the count components LINKS in the
  # designer (same names) - a Label has no click event.
  COUNT_STYLES = {
    'lbl_created_count':     ('Created',         '#2196F3'),  # blue
    'lbl_in_progress_count': ('In Progress',     '#FF9800'),  # amber
    'lbl_complete_count':    ('Complete',        '#4CAF50'),  # green
    'lbl_changes_count':     ('Pending Changes', '#7B2FBE'),  # purple
  }

  def _wire_count_filters(self):
    for attr, (fval, color) in self.COUNT_STYLES.items():
      if not hasattr(self, attr):
        continue
      comp = getattr(self, attr)
      for prop, val in (('foreground', color),
                        ('border', f'1px solid {color}'),
                        ('bold', True),
                        ('role', CHIP_ROLE)):
        if prop == 'role' and not val:
          continue
        try:
          setattr(comp, prop, val)
        except Exception:
          pass
      try:
        comp.set_event_handler('click', lambda f=fval, **e: self._filter_by(f))
      except Exception:
        pass                       # still a Label -> styled, not clickable
    if hasattr(self, 'lbl_record_count'):
      try:
        self.lbl_record_count.set_event_handler(
          'click', lambda **e: self._filter_by('All'))
      except Exception:
        pass

  def _filter_by(self, status_value):
    self.dd_status_filter.selected_value = status_value
    self.refresh_data()

  def _style_count_links(self):
    """Fill the active status chip and outline the rest. Only one status
    filter is active at a time (dd_status_filter); 'All' leaves every chip
    outlined."""
    active = self.dd_status_filter.selected_value
    for attr, (fval, color) in self.COUNT_STYLES.items():
      if not hasattr(self, attr):
        continue
      comp = getattr(self, attr)
      try:
        comp.bold = True
        comp.border = f'1px solid {color}'
        if active == fval:
          comp.background = color
          comp.foreground = '#FFFFFF'
        else:
          comp.background = ''
          comp.foreground = color
      except Exception:
        pass

  # =========================================================================
  # DAILY PLAN  (056: 'Plan' column + chip; click the chip for plan-only)
  # =========================================================================
  def _add_plan_column(self):
    """Insert a 'Plan' column right after WO# without touching the designer:
    a DataGrid column with a data_key and no placed component renders
    item['plan_label'] ('M-3') on its own. Skipped when a column titled
    'Plan' (or keyed plan_label) already exists - e.g. one built in the
    designer with lbl_plan in it, as on the condensed form."""
    try:
      cols = list(self.grid_work_orders.columns or [])
      if any((c.get('data_key') == 'plan_label'
              or (c.get('title') or '').strip().lower() == 'plan') for c in cols):
        return
      cols.insert(1, {'id': 'PLAN056', 'title': 'Plan',
                      'data_key': 'plan_label', 'width': '90'})
      self.grid_work_orders.columns = cols
    except Exception:
      pass

  def _add_desc_column(self):
    """058: a 'Description' column (item['sku_desc'] - the SO line's
    description, the real spec of a non-stock line) right after Part#.
    Code-added unless the designer already has a column titled
    'Description' or keyed sku_desc."""
    try:
      cols = list(self.grid_work_orders.columns or [])
      if any((c.get('data_key') == 'sku_desc'
              or (c.get('title') or '').strip().lower() == 'description') for c in cols):
        return
      after = next((i for i, c in enumerate(cols)
                    if c.get('data_key') == 'part_number'
                    or (c.get('title') or '').strip().lower() == 'part#'), 1)
      cols.insert(after + 1, {'id': 'DESC058', 'title': 'Description',
                              'data_key': 'sku_desc', 'width': '220'})
      self.grid_work_orders.columns = cols
    except Exception:
      pass

  # Column id the row template looks for when it adds its 'View' link.
  # A designer-built column titled 'Routing' takes precedence (any id).
  ROUTING_COL_ID = 'ROUTE0912'

  def _add_routing_column(self):
    """2026-09-12: a 'Routing' column right after Status. The cell holds a
    'View' link (added by WorkOrdersRowTemplate) that pops the swim-lane
    routing diagram for that work order. Code-added unless the designer
    already has a column titled 'Routing'."""
    try:
      cols = list(self.grid_work_orders.columns or [])
      if any((c.get('title') or '').strip().lower() == 'routing' for c in cols):
        return
      after = next((i for i, c in enumerate(cols)
                    if (c.get('title') or '').strip().lower() == 'status'), 1)
      cols.insert(after + 1, {'id': self.ROUTING_COL_ID, 'title': 'Routing',
                              'data_key': '', 'width': '90'})
      self.grid_work_orders.columns = cols
    except Exception:
      pass

  def routing_column_id(self):
    """Id of the Routing column (designer or code-added) for row templates."""
    try:
      for c in (self.grid_work_orders.columns or []):
        if (c.get('title') or '').strip().lower() == 'routing':
          return c.get('id')
    except Exception:
      pass
    return None

  def _wire_plan_filter(self):
    if not hasattr(self, 'lk_plan_count'):
      return
    try:
      self.lk_plan_count.set_event_handler('click', self._toggle_plan)
    except Exception:
      pass
    if CHIP_ROLE:
      try:
        self.lk_plan_count.role = CHIP_ROLE
      except Exception:
        pass
    self._style_plan_link()

  def _toggle_plan(self, **event_args):
    self._plan_only = not self._plan_only
    self.refresh_data()

  def _style_plan_link(self):
    if not hasattr(self, 'lk_plan_count'):
      return
    comp = self.lk_plan_count
    try:
      comp.bold = True
      comp.border = f'1px solid {PLAN_CARD_COLOR}'
      if self._plan_only:
        comp.background = PLAN_CARD_COLOR
        comp.foreground = '#FFFFFF'
      else:
        comp.background = ''
        comp.foreground = PLAN_CARD_COLOR
    except Exception:
      pass

  @staticmethod
  def _plan_sort_val(row):
    """Today's plan first in plan order; everything else after, by WO#."""
    on_plan = bool(row.get('on_todays_plan'))
    return (not on_plan, row.get('plan_position') or 10 ** 9,
            row.get('work_order_number') or '')

  # =========================================================================
  # QUICK RELEASE (2026-10-14): checked set + "Quick Release checked (N)"
  # =========================================================================
  QUICK_BATCH_MAX = 40          # mirrors routing_desk.BATCH_MAX (one call per chunk)

  def _add_quick_release_button(self):
    """A filled button in the summary row, hidden until something is checked.
    Code-added (no designer change); a designer Button named
    btn_quick_release_checked is used instead when present."""
    btn = getattr(self, 'btn_quick_release_checked', None)
    if btn is None:
      btn = Button(text='Quick Release checked (0)', icon='fa:paper-plane',
                   role='filled-button', visible=False)
      try:
        self.flow_summary.add_component(btn)
      except Exception:
        try:
          self.flow_buttons.add_component(btn)
        except Exception:
          return
      self.btn_quick_release_checked = btn
    btn.set_event_handler('click', self._quick_release_checked_click)
    self._style_quick_release_button()

  def toggle_pick(self, work_order_id, checked):
    """Called by the row template's checkbox."""
    if work_order_id is None:
      return
    if checked:
      self._picked.add(work_order_id)
    else:
      self._picked.discard(work_order_id)
    self._style_quick_release_button()

  def note_released(self, n):
    """A row released itself: keep the Plan chip honest without a reload."""
    try:
      txt = self.lk_plan_count.text          # 'Plan: 12/40 released'
      a, b = txt.split(':', 1)[1].strip().split(' ')[0].split('/')
      self.lk_plan_count.text = f"Plan: {int(a) + n}/{b} released"
    except Exception:
      pass

  def _style_quick_release_button(self):
    btn = getattr(self, 'btn_quick_release_checked', None)
    if btn is None:
      return
    n = len(self._picked)
    btn.text = f"Quick Release checked ({n})"
    btn.visible = n > 0

  def _quick_release_checked_click(self, **event_args):
    ids = [w['work_order_id'] for w in self._work_orders
           if w.get('work_order_id') in self._picked and w.get('status') == 'Created']
    skipped = len(self._picked) - len(ids)
    if not ids:
      Notification('Nothing checked that is still Created.').show()
      return
    msg = (f"Release {len(ids)} work order{'s' if len(ids) != 1 else ''}?\n\n"
           f"Each keeps its saved routing: step 1 goes Ready on that department's board, "
           f"subscribed departments are texted, nothing is edited.")
    if skipped:
      msg += f"\n\n{skipped} checked but no longer Created - skipped."
    if not confirm(msg):
      return
    btn = self.btn_quick_release_checked
    btn.enabled = False
    released, failed, messages = 0, 0, []
    try:
      for i in range(0, len(ids), self.QUICK_BATCH_MAX):
        chunk = ids[i:i + self.QUICK_BATCH_MAX]
        res = anvil.server.call('release_work_orders', chunk, None, 'quick-batch')
        if res.get('status') != 'success':
          failed += len(chunk)
          messages.append(res.get('message') or 'batch failed')
          continue
        released += int(res.get('released') or 0)
        failed += int(res.get('failed') or 0)
        by_id = {w['work_order_id']: w for w in self._work_orders}
        for one in res.get('results') or []:
          if one.get('status') != 'success':
            w = by_id.get(one.get('work_order_id'))
            messages.append(f"{w['work_order_number'] if w else one.get('work_order_id')}: "
                            f"{one.get('message') or 'failed'}")
    finally:
      btn.enabled = True
    self._picked = set()
    summary = f"Released {released}" + (f", {failed} failed" if failed else '')
    if messages:
      alert('\n'.join([summary, ''] + messages[:20]), title='Quick Release')
    else:
      Notification(summary).show()
    self.refresh_data()

  # =========================================================================
  # SORTING
  # =========================================================================
  def sort_by(self, key):
    """Sort work orders by a given key. Toggle direction on repeat click."""
    if self._sort_key == key:
      self._sort_reverse = not self._sort_reverse
    else:
      self._sort_key = key
      self._sort_reverse = False

    def sort_val(row):
      if key == 'plan_sort':
        return self._plan_sort_val(row)
      val = row.get(key)
      if val is None or val == '':
        return '' if isinstance(val, str) else 0

      # Parse date strings for proper chronological sorting
      if key in ('due_date', 'created_at'):
        try:
          from datetime import datetime as dt
          return dt.strptime(val, '%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          try:
            return dt.strptime(val, '%m/%d/%Y')
          except (ValueError, TypeError):
            return val
      return val

    self._work_orders.sort(key=sort_val, reverse=self._sort_reverse)
    self._show_rows()

  def _reapply_sort(self):
    """Re-apply the current dd_sort selection to self._work_orders.
    Called after refresh_data loads fresh server data."""
    selected = (self.dd_sort.selected_value or '').strip()
    if not selected:
      return

    key = SORT_MAP.get(selected)
    if not key:
      return

    # Preserve the existing sort direction
    reverse = self._sort_reverse

    def sort_val(row):
      if key == 'plan_sort':
        return self._plan_sort_val(row)
      val = row.get(key)
      if val is None or val == '':
        return '' if isinstance(val, str) else 0
      if key in ('due_date', 'created_at'):
        try:
          from datetime import datetime as dt
          return dt.strptime(val, '%m/%d/%Y %I:%M %p')
        except (ValueError, TypeError):
          try:
            return dt.strptime(val, '%m/%d/%Y')
          except (ValueError, TypeError):
            return val
      return val

    self._work_orders.sort(key=sort_val, reverse=reverse)

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  @handle("dd_status_filter", "change")
  def dd_status_filter_change(self, **event_args):
    """Filter changed — reload data."""
    self.refresh_data()

  @handle("tb_search", "pressed_enter")
  def tb_search_pressed_enter(self, **event_args):
    """Search box enter pressed — reload data."""
    self.refresh_data()

  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    """Refresh button clicked — reload data."""
    self.refresh_data()
    Notification("Refreshed").show()



  @handle("btn_scan", "click")
  def btn_scan_click(self, **event_args):
    """Scan for special configs and create work orders."""
    self.btn_scan.enabled = False
    self.btn_scan.text = "Scanning..."
    try:
      result = anvil.server.call('scan_and_create_work_orders')
      if result['status'] == 'success':
        Notification(result['message']).show()
        if result['errors']:
          alert("Errors:\n" + "\n".join(result['errors']), title="Scan Warnings")
        if result['created'] > 0:
          self.refresh_data()
      else:
        alert(f"Error: {result['message']}")
    finally:
      self.btn_scan.enabled = True
      self.btn_scan.text = "Scan for New Work Orders"

  @handle("dd_sort", "change")
  def dd_sort_change(self, **event_args):
    # .strip(): the designer list has 'WO# (Ascending) ' with a trailing space
    selected = (self.dd_sort.selected_value or '').strip()
    key = SORT_MAP.get(selected)
    if key:
      # Created Date (Newest) always sorts descending (newest first)
      if selected == 'Created Date (Newest)':
        self._sort_key = key
        self._sort_reverse = True

        def sort_val(row):
          val = row.get(key)
          if val is None or val == '':
            return ''
          try:
            from datetime import datetime as dt
            return dt.strptime(val, '%m/%d/%Y %I:%M %p')
          except (ValueError, TypeError):
            try:
              return dt.strptime(val, '%m/%d/%Y')
            except (ValueError, TypeError):
              return val

        self._work_orders.sort(key=sort_val, reverse=True)
        self._show_rows()
      else:
        self.sort_by(key)

  @handle("cb_active_only", "change")
  def cb_active_only_change(self, **event_args):
    self.refresh_data()

  @handle("cb_backorder_only", "change")
  def cb_backorder_only_change(self, **event_args):
    """Backorders-only filter toggled — reload data."""
    self.refresh_data()


  @handle("btn_ack_orphans", "click")
  def btn_ack_orphans_click(self, **event_args):
    """Bulk acknowledge all orphaned Verify-status work orders."""
    if not confirm(
      "Acknowledge all orphaned work orders?\n\n"
      "This will mark all work orders in Shipped - Verify, "
      "Invoiced - Verify, and Closed - Verify as Complete, "
      "and acknowledge changes on Cancelled work orders.\n\n"
      "This is a testing-phase convenience action."
    ):
      return

    self.btn_ack_orphans.enabled = False
    self.btn_ack_orphans.text = "Processing..."
    try:
      result = anvil.server.call('bulk_acknowledge_verify_work_orders')
      if result['status'] == 'success':
        Notification(result.get('message', 'Done')).show()
        if result.get('acknowledged', 0) > 0:
          self.refresh_data()
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")
    finally:
      self.btn_ack_orphans.enabled = True
      self.btn_ack_orphans.text = "Accept Orphans"

  @handle("btn_sms_test", "click")
  def btn_sms_test_click(self, **event_args):
    """This method is called when the button is clicked"""
    anvil.server.call('send_routing_notifications_now')