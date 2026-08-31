from ._anvil_designer import work_ordersTemplate
from anvil import *
import anvil.server


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

    # Hide the old inline detail panel (kept in YAML for rollback, hidden permanently)
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

      # Re-apply active sort if one is selected
      self._reapply_sort()

      self.repeating_panel_1.items = self._work_orders

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
    self.repeating_panel_1.items = self._work_orders

  def _reapply_sort(self):
    """Re-apply the current dd_sort selection to self._work_orders.
    Called after refresh_data loads fresh server data."""
    selected = self.dd_sort.selected_value
    if not selected:
      return

    sort_map = {
      'Due Date (Earliest)': 'due_date',
      'SO# (Ascending)': 'sales_order_number',
      'WO# (Ascending)': 'work_order_number',
      'Part# (Ascending)': 'part_number',
      'Created Date (Newest)': 'created_at',
    }
    key = sort_map.get(selected)
    if not key:
      return

    # Preserve the existing sort direction
    reverse = self._sort_reverse

    def sort_val(row):
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
    sort_map = {
      'Due Date (Earliest)': 'due_date',
      'SO# (Ascending)': 'sales_order_number',
      'WO# (Ascending)': 'work_order_number',
      'Part# (Ascending)': 'part_number',
      'Created Date (Newest)': 'created_at',
    }
    selected = self.dd_sort.selected_value
    key = sort_map.get(selected)
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
        self.repeating_panel_1.items = self._work_orders
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
