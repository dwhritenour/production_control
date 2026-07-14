"""
Anvil Client Form - Sales Orders Due
====================================
Production Control–facing read-only view of open sales order lines, with
emphasis on identifying late and at-risk orders. Pulls from the Nexus
table dbo.sales_orders_due via sales_orders_due_dashboard server module.

This is a READ-ONLY form. No actions on this form modify any e21 data.

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: May 2026

Version: v5.18.0 (2026-06-19)
  - Method of Shipment + Rush (PC request). Two new grid columns
    (Carrier, MOS) bound to nx_carrier / nx_mos, placed right after
    Ship-To. New cb_rush_only filter checkbox (mirrors cb_diverged_only)
    that restricts the grid to rush ship-method orders. New lk_rush_count
    summary card, colored burnt-orange (#E65100, RUSH_CARD_COLOR); clicking
    it checks cb_rush_only. Row template adds a lightning (⚡) glyph at the
    end of the Order # cell for rush lines (glyph-only, mirroring the ⚑
    flag; the color signal lives on the card, not the grid cell).
  - Requires server v5.21.0+ (show_rush_only kwarg, 'rush' count,
    nx_carrier/nx_mos/is_rush fields) and row template v5.17.0+.
  - Designer prerequisites (manual Anvil build):
    * Two DataGrid columns on dg_lines: Carrier (data key nx_carrier)
      then MOS (data key nx_mos), inserted right after Ship-To.
    * CheckBox cb_rush_only in flow_filters, beside cb_ship_by_stale_only.
    * Link lk_rush_count in the summary-cards row, beside
      lk_ship_by_stale_count.

Version: v5.17.0 (2026-05-19)
  - "Created on" date filter. New dp_created_on DatePicker in
    flow_filters; when set, the grid is filtered to lines whose SO
    header was created on that calendar day. AND-combines with all
    other filters. Cleared by btn_clear_filters. Passed through to
    Excel export.
  - Requires server v5.19.0+ (new created_on_date kwarg on
    get_sales_orders_due / export_sales_orders_due_excel).

Version: v5.16.0 (2026-05-19)
  - Adds visual grouping of grid rows by order_release (order_numb +
    rel_numb). Adjacent rows that share the same order_release get the
    same band color; the band color flips whenever order_release
    changes. Grouping is recomputed after every sort, so it stays
    correct when the user re-sorts columns.
  - Backorder releases (e.g. 598195-002) intentionally get their own
    band, separate from the parent (598195-001). Per Dave on
    2026-05-19, each release is treated as a distinct unit of work.
  - Two soft band tints are assigned via item['band_index'] = 0 or 1.
    The row template applies the actual background color. No server
    changes; no schema changes.

Version: v5.15.0 (2026-05-19)
  - Stale Ship-By card, checkbox, handler. See
    Sales_Orders_Due_StaleShipBy_Form_Specification.docx.

Prerequisites still in effect from prior versions (manual Anvil Designer):
  * Link component lk_ship_by_stale_count in the summary-cards row
  * CheckBox cb_ship_by_stale_only in the filter row
v5.17.0 designer prerequisites:
  * Label "Created on:" in flow_filters
  * DatePicker dp_created_on in flow_filters (placed after that Label)
"""

from ._anvil_designer import sales_orders_dueTemplate
from anvil import *
import anvil.server
import anvil.media
from datetime import datetime


# View-mode labels (must match the strings the server understands)
VIEW_MODE_DEFAULT   = 'Late + Next 7 Days'
VIEW_MODE_LATE_ONLY = 'Late Only'
VIEW_MODE_DUE_TODAY = 'Due Today'
VIEW_MODE_NEXT_30   = 'Next 30 Days'
VIEW_MODE_ALL_OPEN  = 'All Open'
VIEW_MODE_CUSTOM    = 'Custom Range'


# v5.18.0 — rush summary-card accent color.
# Burnt-orange (#E65100): a strong, warm "urgent" orange — think a safety
# cone / amber-red. Chosen to stand apart from the red used for late days
# (#D32F2F) and the amber for due-today (#FF9800). Set in code (not the
# Designer) per the team's color-in-code convention. The card carries the
# rush COLOR; the grid row carries only the ⚡ glyph.
RUSH_CARD_COLOR = '#E65100'


# v5.16.0 — order-release banding.
# The row template reads item['band_index'] (0 or 1) and applies the
# matching background color. Tints are subtle on purpose so they don't
# fight the per-cell foreground coloring (red Days, blue Allocated, etc.).
def _apply_order_grouping_bands(lines):
  """Assign item['band_index'] = 0 or 1 to each row, flipping whenever
  the order_release changes from the previous row. Mutates in place.

  Grouping key is order_release (order_numb-rel_numb) so the parent SO
  and its backorder release(s) form distinct visual groups.
  """
  if not lines:
    return
  current_band = 0
  prev_key = object()  # sentinel — first row always starts a fresh band at 0
  for ln in lines:
    key = ln.get('order_release')
    if key != prev_key and prev_key is not object():
      # New order_release — flip the band.
      current_band = 1 - current_band
    ln['band_index'] = current_band
    prev_key = key


class sales_orders_due(sales_orders_dueTemplate):

  def __init__(self, **properties):
    # ---------------------------------------------------------------
    # Anvil setup
    # ---------------------------------------------------------------
    self.init_components(**properties)
    self.dg_lines.role = ['tonal-data-grid', 'horizontal-scroll']
    self.nav.active = 'sales_orders_due'

    # v5.18.0 — give the Rush summary card its accent color (set once;
    # only the text is updated on each refresh).
    self.lk_rush_count.foreground = RUSH_CARD_COLOR

    # Internal state
    self._lines = []
    self._sort_key = 'days_to_ship'
    self._sort_reverse = False

    # Custom date range pickers stay hidden unless 'Custom Range' is picked
    self.dp_custom_start.visible = False
    self.dp_custom_end.visible = False

    # Default view-mode
    if not self.dd_view_mode.selected_value:
      self.dd_view_mode.selected_value = VIEW_MODE_DEFAULT

    # ---------------------------------------------------------------
    # Wire event handlers
    # ---------------------------------------------------------------
    # Page-level buttons
    self.btn_refresh.set_event_handler('click',         self.btn_refresh_click)
    self.btn_export_excel.set_event_handler('click',    self.btn_export_excel_click)
    self.btn_clear_filters.set_event_handler('click',   self.btn_clear_filters_click)

    # Filters
    self.dd_view_mode.set_event_handler('change',       self.dd_view_mode_change)
    self.dp_custom_start.set_event_handler('change',    self.filter_changed)
    self.dp_custom_end.set_event_handler('change',      self.filter_changed)
    self.dd_status_filter.set_event_handler('change',   self.filter_changed)
    self.tb_search.set_event_handler('pressed_enter',   self.filter_changed)
    self.cb_committed_only.set_event_handler('change',  self.filter_changed)
    self.cb_diverged_only.set_event_handler('change',   self.filter_changed)
    # v5.15.0
    self.cb_ship_by_stale_only.set_event_handler('change', self.filter_changed)
    # v5.17.0
    self.dp_created_on.set_event_handler('change', self.filter_changed)
    # v5.18.0
    self.cb_rush_only.set_event_handler('change', self.filter_changed)

    # Summary card click handlers (Link components — emit 'click' natively)
    self.lk_late_ship_count.set_event_handler('click', self.set_view_mode_late_only)
    self.lk_due_today_count.set_event_handler('click', self.set_view_mode_due_today)
    self.lk_due_7_count.set_event_handler('click',     self.set_view_mode_default)
    self.lk_on_hold_count.set_event_handler('click',   self.set_view_mode_on_hold)
    self.lk_diverged_count.set_event_handler('click',  self.set_view_mode_diverged)
    # v5.15.0
    self.lk_ship_by_stale_count.set_event_handler('click', self.set_view_mode_ship_by_stale)
    # v5.18.0
    self.lk_rush_count.set_event_handler('click', self.set_view_mode_rush)

    # Initial load
    self.refresh_data()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def refresh_data(self):
    """Primary data load. Called from init, refresh button, and any
    filter change."""
    view_mode      = self.dd_view_mode.selected_value or VIEW_MODE_DEFAULT
    status_filter  = self.dd_status_filter.selected_value
    if status_filter in (None, '', 'All'):
      status_filter = None
    search_text    = (self.tb_search.text or '').strip() or None
    custom_start   = self.dp_custom_start.date if view_mode == VIEW_MODE_CUSTOM else None
    custom_end     = self.dp_custom_end.date   if view_mode == VIEW_MODE_CUSTOM else None

    result = anvil.server.call(
      'get_sales_orders_due',
      view_mode=view_mode,
      status_filter=status_filter,
      search_text=search_text,
      has_allocated_only=self.cb_committed_only.checked,
      show_diverged_only=self.cb_diverged_only.checked,
      show_ship_by_stale_only=self.cb_ship_by_stale_only.checked,
      show_rush_only=self.cb_rush_only.checked,
      created_on_date=self.dp_created_on.date,
      custom_start=custom_start,
      custom_end=custom_end,
    )

    if result['status'] != 'success':
      alert(f"Error loading sales orders: {result.get('message', 'Unknown error')}")
      return

    self._lines = result['lines']
    self._reapply_sort()
    # v5.16.0 — band AFTER sort so adjacency is correct
    _apply_order_grouping_bands(self._lines)
    self.rp_lines.items = self._lines

    # Record-count summary
    count = len(self._lines)
    qty_total = sum((ln.get('qty_balance_due') or 0) for ln in self._lines)
    self.lbl_record_count.text = f"{count} lines  |  {qty_total:,.0f} qty open"

    # Summary cards (UNFILTERED counts from the server)
    c = result['counts']
    self.lk_late_ship_count.text     = f"Late (Ship): {c.get('late_ship', 0)}"
    self.lk_due_today_count.text     = f"Due Today: {c.get('due_today', 0)}"
    self.lk_due_7_count.text         = f"Due in 7 Days: {c.get('due_7', 0)}"
    self.lk_on_hold_count.text       = f"On Hold: {c.get('on_hold', 0)}"
    self.lk_diverged_count.text      = f"Diverged Dates: {c.get('diverged_dates', 0)}"
    self.lk_ship_by_stale_count.text = f"Stale Ship-By: {c.get('ship_by_stale', 0)}"
    self.lk_rush_count.text          = f"Rush: {c.get('rush', 0)}"

    aging = c.get('aging', {})
    self.lbl_aging_summary.text = (
      f"Aging — 1-7d: {aging.get('1_7', 0)}  |  "
      f"8-30d: {aging.get('8_30', 0)}  |  "
      f"31+d: {aging.get('31_plus', 0)}"
    )

    self.lbl_last_refreshed.text = (
      f"Last Refreshed: {datetime.now().strftime('%I:%M %p')}"
    )

  # =========================================================================
  # FILTER / VIEW-MODE HANDLERS
  # =========================================================================

  def filter_changed(self, **event_args):
    """Generic handler for any filter component that should trigger reload."""
    self.refresh_data()

  def dd_view_mode_change(self, **event_args):
    """Show/hide custom-range pickers based on the selected view-mode."""
    is_custom = (self.dd_view_mode.selected_value == VIEW_MODE_CUSTOM)
    self.dp_custom_start.visible = is_custom
    self.dp_custom_end.visible   = is_custom
    if is_custom and (not self.dp_custom_start.date or not self.dp_custom_end.date):
      return
    self.refresh_data()

  # ---- Summary-card click handlers ----------------------------------------

  def set_view_mode_default(self, **event_args):
    """'Due in 7 Days' card click — restores the default view."""
    self.dd_view_mode.selected_value = VIEW_MODE_DEFAULT
    self.dp_custom_start.visible = False
    self.dp_custom_end.visible = False
    self.refresh_data()

  def set_view_mode_late_only(self, **event_args):
    self.dd_view_mode.selected_value = VIEW_MODE_LATE_ONLY
    self.dp_custom_start.visible = False
    self.dp_custom_end.visible = False
    self.refresh_data()

  def set_view_mode_due_today(self, **event_args):
    self.dd_view_mode.selected_value = VIEW_MODE_DUE_TODAY
    self.dp_custom_start.visible = False
    self.dp_custom_end.visible = False
    self.refresh_data()

  def set_view_mode_on_hold(self, **event_args):
    """'On Hold' card click — keeps current view-mode but pins status to Hold."""
    self.dd_status_filter.selected_value = 'Hold'
    self.refresh_data()

  def set_view_mode_diverged(self, **event_args):
    """'Diverged Dates' card click — checks the diverged-only checkbox."""
    self.cb_diverged_only.checked = True
    self.refresh_data()

  def set_view_mode_ship_by_stale(self, **event_args):
    """'Stale Ship-By' card click — checks the stale-only checkbox."""
    self.cb_ship_by_stale_only.checked = True
    self.refresh_data()

  def set_view_mode_rush(self, **event_args):
    """'Rush' card click — checks the rush-only checkbox."""
    self.cb_rush_only.checked = True
    self.refresh_data()

  # ---- Clear filters ------------------------------------------------------

  def btn_clear_filters_click(self, **event_args):
    self.dd_view_mode.selected_value = VIEW_MODE_DEFAULT
    self.dd_status_filter.selected_value = 'All'
    self.tb_search.text = ''
    self.cb_committed_only.checked = False
    self.cb_diverged_only.checked = False
    self.cb_ship_by_stale_only.checked = False
    self.cb_rush_only.checked = False
    self.dp_created_on.date = None
    self.dp_custom_start.date = None
    self.dp_custom_end.date = None
    self.dp_custom_start.visible = False
    self.dp_custom_end.visible = False
    self.refresh_data()

  # ---- Refresh button -----------------------------------------------------

  def btn_refresh_click(self, **event_args):
    self.refresh_data()

  # =========================================================================
  # SORTING
  # =========================================================================

  def sort_by(self, key):
    """Sort lines by a given key. Toggle direction on repeat click."""
    if self._sort_key == key:
      self._sort_reverse = not self._sort_reverse
    else:
      self._sort_key = key
      self._sort_reverse = False

    def sort_val(row):
      val = row.get(key)
      if val is None or val == '':
        return '' if isinstance(val, str) else 0
      if key in ('ship_by_date', 'due_date', 'header_ship_by', 'header_due'):
        try:
          return datetime.strptime(val, '%m/%d/%Y')
        except (ValueError, TypeError):
          return val
      return val

    self._lines.sort(key=sort_val, reverse=self._sort_reverse)
    # v5.16.0 — re-band after every sort so order groups stay coherent
    _apply_order_grouping_bands(self._lines)
    self.rp_lines.items = self._lines

  def _reapply_sort(self):
    """Re-apply the current sort_key (and direction) after a server reload."""
    key = self._sort_key
    if not key or not self._lines:
      return

    def sort_val(row):
      val = row.get(key)
      if val is None or val == '':
        return '' if isinstance(val, str) else 0
      if key in ('ship_by_date', 'due_date', 'header_ship_by', 'header_due'):
        try:
          return datetime.strptime(val, '%m/%d/%Y')
        except (ValueError, TypeError):
          return val
      return val

    self._lines.sort(key=sort_val, reverse=self._sort_reverse)
    # NOTE: banding is applied by the caller (refresh_data) after this
    # method returns. _reapply_sort is internal — the public entry points
    # (refresh_data and sort_by) own the band step.

  # =========================================================================
  # EXCEL EXPORT
  # =========================================================================

  def btn_export_excel_click(self, **event_args):
    """Build the filter dict from current form state, call the server
    export function, and trigger a browser download."""
    view_mode = self.dd_view_mode.selected_value or VIEW_MODE_DEFAULT
    status_filter = self.dd_status_filter.selected_value
    if status_filter in (None, '', 'All'):
      status_filter = None
    filters = {
      'view_mode':              view_mode,
      'status_filter':          status_filter,
      'search_text':            (self.tb_search.text or '').strip() or None,
      'has_allocated_only':     self.cb_committed_only.checked,
      'show_diverged_only':     self.cb_diverged_only.checked,
      'show_ship_by_stale_only': self.cb_ship_by_stale_only.checked,
      'show_rush_only':         self.cb_rush_only.checked,
      'created_on_date':        self.dp_created_on.date,
      'custom_start':           self.dp_custom_start.date if view_mode == VIEW_MODE_CUSTOM else None,
      'custom_end':             self.dp_custom_end.date   if view_mode == VIEW_MODE_CUSTOM else None,
    }

    # Disable the button briefly so the user doesn't double-click during the round-trip
    self.btn_export_excel.enabled = False
    self.btn_export_excel.text = 'Building…'
    try:
      result = anvil.server.call('export_sales_orders_due_excel', filters=filters)
    finally:
      self.btn_export_excel.enabled = True
      self.btn_export_excel.text = 'Export to Excel'

    if result['status'] != 'success':
      alert(f"Export failed: {result.get('message', 'Unknown error')}")
      return

    anvil.media.download(result['media'])
    Notification(f"Exported {result.get('row_count', 0)} lines.").show()