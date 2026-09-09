from ._anvil_designer import work_orders_condensedTemplate
from anvil import *
import anvil.server


# Condensed "Active Work Orders" view (built for the 2026-06-17 Production
# rollout). Shows only the statuses Production works day-to-day. Edit this one
# line to also surface floor states (e.g. add 'Assigned', 'Paused').
ACTIVE_STATUSES = ['Created', 'Released', 'Assigned', 'In Progress', 'Paused']

# Rows per page for the custom pager (tunable).
ROWS_PER_PAGE = 15

# Rush summary-card accent (matches sales_orders_due RUSH_CARD_COLOR).
RUSH_CARD_COLOR = '#E65100'

# Daily Plan chip accent (056): today's published plan, released / total.
PLAN_CARD_COLOR = '#1B5E20'

SORT_MAP = {
  'Plan (position)': 'plan_sort',
  'Due Date (Earliest)': 'wo_due_date',
  'SO Due Date (Earliest)': 'due_date',
  'SO# (Ascending)': 'sales_order_number',
  'WO# (Ascending)': 'work_order_number',
  'Part# (Ascending)': 'part_number',
}


class work_orders_condensed(work_orders_condensedTemplate):
  """
    Condensed, read-mostly Active Work Orders grid.

    Columns: WO# | Status | SO# | Part# | Material / SE | Qty | Override Qty |
             WO Due Date | SO Due Date
    All actions (Release / Cancel / Print / SO packet) live in the WorkOrderDetail
    popup, opened by clicking the WO# link. Notes also live in that popup.
  """

  def __init__(self, **properties):
    self.init_components(**properties)
    self.grid_active.role = ['tonal-data-grid', 'horizontal-scroll']
    # Status filter limited to the active set (placeholder/'All' = all active)
    self.dd_status_filter.items = ['All', 'Created', 'Released',
                                   'Assigned', 'In Progress', 'Paused',
                                   'Pending Changes']
    self.dd_status_filter.selected_value = 'All'
    # Sort options pinned in code so they always match SORT_MAP keys
    self.dd_sort.items = list(SORT_MAP.keys())
    self._wire_count_filters()
    self._rush_only = False
    self._wire_rush_filter()
    self._plan_only = False
    self._wire_plan_filter()
    self._add_plan_column()
    self._page = 0
    self._wire_pager()
    try:
      self.nav.active = 'work_orders_condensed'
    except Exception:
      pass
    self._work_orders = []
    self.refresh_data()

  # =========================================================================
  # DATA
  # =========================================================================
  def refresh_data(self):
    search_text = (self.tb_search.text or '').strip() or None
    status_sel = self.dd_status_filter.selected_value
    if status_sel and status_sel != 'All':
      # One specific active status chosen.
      result = anvil.server.call('get_work_orders',
                                 status_filter=status_sel,
                                 search_text=search_text,
                                 show_rush_only=self._rush_only)
    else:
      # All active statuses.
      result = anvil.server.call('get_work_orders',
                                 statuses=ACTIVE_STATUSES,
                                 search_text=search_text,
                                 show_rush_only=self._rush_only)
    if result['status'] != 'success':
      alert(f"Error loading work orders: {result.get('message', 'Unknown error')}")
      return

    self._work_orders = result['work_orders']
    if self._plan_only:
      # 056: only today's published plan (server already sorted it first)
      self._work_orders = [w for w in self._work_orders if w.get('on_todays_plan')]
    self._apply_sort()
    self._page = 0
    self._render_page()

    counts = result.get('counts', {})
    # Set whichever count labels exist (guarded so the form loads even before
    # the Assigned/Paused labels are added in the designer).
    def _setcount(attr, label, val):
      if hasattr(self, attr):
        getattr(self, attr).text = f"{label}: {val}"
    _setcount('lbl_created_count',     'Created',     counts.get('Created', 0))
    _setcount('lbl_released_count',    'Released',    counts.get('Released', 0))
    _setcount('lbl_assigned_count',    'Assigned',    counts.get('Assigned', 0))
    _setcount('lbl_in_progress_count', 'In Progress', counts.get('In Progress', 0))
    _setcount('lbl_paused_count',      'Paused',      counts.get('Paused', 0))
    _setcount('lbl_changes_count',     'Changes',     counts.get('PendingChanges', 0))
    if hasattr(self, 'lk_rush_count'):
      self.lk_rush_count.text = f"Rush: {counts.get('rush', 0)}"
    self._style_rush_link()
    if hasattr(self, 'lk_plan_count'):
      self.lk_plan_count.text = (f"Plan: {counts.get('plan_released', 0)}/"
                                 f"{counts.get('plan_today', 0)} released")
    self._style_plan_link()
    self._style_count_links()
    self.lbl_record_count.text = f"{len(self._work_orders)} active work orders"

    from datetime import datetime
    self.lbl_last_refreshed.text = (
      f"Last Refreshed: {datetime.now().strftime('%I:%M %p')}")

  # =========================================================================
  # CLICKABLE COUNT FILTERS  (click a count to apply that filter)
  # =========================================================================
  # attr -> (filter value, color). Colors match the work-order ROW status
  # colors so the whole app is consistent. Set in code so they never need to be
  # picked by hand in the designer.
  COUNT_STYLES = {
    'lbl_created_count':     ('Created',         '#2196F3'),  # blue
    'lbl_released_count':    ('Released',        '#00897B'),  # teal
    'lbl_assigned_count':    ('Assigned',        '#607D8B'),  # blue-gray
    'lbl_in_progress_count': ('In Progress',     '#FF9800'),  # amber
    'lbl_paused_count':      ('Paused',          '#C62828'),  # red
    'lbl_changes_count':     ('Pending Changes', '#7B2FBE'),  # purple
  }

  def _wire_count_filters(self):
    # Styling + click is applied defensively: a plain Label has no 'click'
    # event (only Link/Button do), so each step is guarded. Make the count
    # components LINKS in the designer (keep the same names) to enable click.
    for attr, (fval, color) in self.COUNT_STYLES.items():
      if not hasattr(self, attr):
        continue
      comp = getattr(self, attr)
      for prop, val in (('foreground', color),
                        ('border', f'1px solid {color}'),
                        ('bold', True)):
        try:
          setattr(comp, prop, val)
        except Exception:
          pass
      try:
        comp.set_event_handler('click', lambda f=fval, **e: self._filter_by(f))
      except Exception:
        pass
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
    """Fill the active status chip and outline the rest, mirroring the Rush
    chip's active-state feedback. Only one status filter is active at a time
    (dd_status_filter), so 'All' leaves every chip outlined."""
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
  # RUSH FILTER  (click the Rush link to toggle rush-only)
  # =========================================================================
  def _wire_rush_filter(self):
    """Style + wire the Rush summary link. Guarded so the form still loads
    before lk_rush_count is added in the designer."""
    if not hasattr(self, 'lk_rush_count'):
      return
    try:
      self.lk_rush_count.set_event_handler('click', self._toggle_rush)
    except Exception:
      pass
    self._style_rush_link()

  def _toggle_rush(self, **event_args):
    self._rush_only = not self._rush_only
    self.refresh_data()

  def _style_rush_link(self):
    """Reflect active/inactive state on the Rush link (filled when active)."""
    if not hasattr(self, 'lk_rush_count'):
      return
    comp = self.lk_rush_count
    try:
      comp.bold = True
      comp.border = f'1px solid {RUSH_CARD_COLOR}'
      if self._rush_only:
        comp.background = RUSH_CARD_COLOR
        comp.foreground = '#FFFFFF'
      else:
        comp.background = ''
        comp.foreground = RUSH_CARD_COLOR
    except Exception:
      pass

  # =========================================================================
  # DAILY PLAN  (056: 'Plan' column + chip; click the chip for plan-only)
  # =========================================================================
  def _add_plan_column(self):
    """Insert a 'Plan' column right after WO# without touching the designer:
    a DataGrid column with a data_key and no placed component renders
    item['plan_label'] ('M-3') on its own. Skipped if a column with that
    data_key already exists (e.g. one you added in the designer)."""
    try:
      cols = list(self.grid_active.columns or [])
      # Dave built a 'Plan' column in the designer (2026-09-08) with lbl_plan
      # in it - never add a second one. Match on title OR data_key.
      if any((c.get('data_key') == 'plan_label'
              or (c.get('title') or '').strip().lower() == 'plan') for c in cols):
        return
      cols.insert(1, {'id': 'PLAN056', 'title': 'Plan',
                      'data_key': 'plan_label', 'width': '70'})
      self.grid_active.columns = cols
    except Exception:
      pass

  def _wire_plan_filter(self):
    if not hasattr(self, 'lk_plan_count'):
      return
    try:
      self.lk_plan_count.set_event_handler('click', self._toggle_plan)
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

  # =========================================================================
  # CUSTOM PAGINATION  ("Showing X to Y of N entries")
  # =========================================================================
  def _wire_pager(self):
    # 2026-09-09: the footer is now the GridPager CUSTOM COMPONENT. If one
    # named `pager` sits under the grid it owns paging and oc_footer can be
    # deleted; the inline footer below stays as the fallback.
    if hasattr(self, 'pager'):
      try:
        self.pager.attach(self.grid_active, self.repeating_panel_1)
        return
      except Exception:
        pass
    # Only take over paging if the pager components exist; otherwise leave the
    # DataGrid's built-in pager alone (safe to paste before building them).
    if all(hasattr(self, a) for a in ('lk_prev', 'lk_next', 'lbl_paging')):
      self.grid_active.rows_per_page = 0      # we paginate manually
      self.lk_prev.text = '‹ Prev'
      self.lk_next.text = 'Next ›'
      self.lk_prev.set_event_handler('click', lambda **e: self._change_page(-1))
      self.lk_next.set_event_handler('click', lambda **e: self._change_page(1))
      # Optional First / Last controls (wired only if present)
      if hasattr(self, 'lk_first'):
        self.lk_first.text = '« First'
        self.lk_first.set_event_handler('click', lambda **e: self._goto_page(0))
      if hasattr(self, 'lk_last'):
        self.lk_last.text = 'Last »'
        self.lk_last.set_event_handler('click', lambda **e: self._goto_page(10 ** 9))

  def _change_page(self, delta):
    self._page += delta
    self._render_page()

  def _goto_page(self, target):
    # _render_page clamps target into range, so a huge number == last page.
    self._page = target
    self._render_page()

  def _render_page(self):
    if hasattr(self, 'pager'):
      try:
        self.pager.items = self._work_orders      # component paginates (page -> 1)
        return
      except Exception:
        pass
    have_pager = all(hasattr(self, a) for a in ('lk_prev', 'lk_next', 'lbl_paging'))
    total = len(self._work_orders)
    if not have_pager:
      # Pager not built yet -> show everything (built-in paging still applies).
      self.repeating_panel_1.items = self._work_orders
      return
    pages = max(1, (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
    self._page = max(0, min(self._page, pages - 1))
    start = self._page * ROWS_PER_PAGE
    end = min(start + ROWS_PER_PAGE, total)
    self.repeating_panel_1.items = self._work_orders[start:end]
    first = start + 1 if total else 0
    self.lbl_paging.text = f"Showing {first} to {end} of {total} entries"
    self.lk_prev.visible = self._page > 0
    self.lk_next.visible = end < total
    if hasattr(self, 'lk_first'):
      self.lk_first.visible = self._page > 0
    if hasattr(self, 'lk_last'):
      self.lk_last.visible = end < total

  # =========================================================================
  # SORT  (client-side; manual date parse -- strptime suspends in a sort key)
  # =========================================================================
  def _apply_sort(self):
    selected = self.dd_sort.selected_value
    key = SORT_MAP.get(selected)
    if not key:
      return

    def parse_mdY(val):
      try:
        m, d, y = val.split('/')
        return (int(y), int(m), int(d))
      except (AttributeError, ValueError):
        return None

    def sort_val(row):
      if key == 'plan_sort':
        # today's plan first in plan order; everything else after, by WO#
        on_plan = bool(row.get('on_todays_plan'))
        return (not on_plan, row.get('plan_position') or 10 ** 9,
                row.get('work_order_number') or '')
      val = row.get(key)
      if key in ('wo_due_date', 'due_date'):
        return (parse_mdY(val) is None, parse_mdY(val) or (9999, 12, 31))
      return (val is None or val == '', val or '')

    self._work_orders.sort(key=sort_val)

  # =========================================================================
  # EVENTS
  # =========================================================================
  @handle("dd_sort", "change")
  def dd_sort_change(self, **event_args):
    self._apply_sort()
    self._page = 0
    self._render_page()

  @handle("dd_status_filter", "change")
  def dd_status_filter_change(self, **event_args):
    self.refresh_data()

  @handle("tb_search", "pressed_enter")
  def tb_search_pressed_enter(self, **event_args):
    self.refresh_data()

  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    self.refresh_data()
    Notification("Refreshed").show()

  @handle("btn_clear_filters", "click")
  def btn_clear_filters_click(self, **event_args):
    self.tb_search.text = ''
    self.dd_sort.selected_value = None
    self.dd_status_filter.selected_value = 'All'
    self._rush_only = False
    self.refresh_data()