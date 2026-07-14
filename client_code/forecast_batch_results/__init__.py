"""
forecast_batch_results - Batch Results Viewer
================================================
Browse SKU-level forecast results for a specific batch.
Filters: batch (dropdown), SKU (partial), series (dropdown), month (1-12).

Defaults to the most recent batch with no filters applied.

Server module: forecast_display.py
  - get_recent_batches()
  - get_batch_series_list(batch_id)
  - get_forecast_batch_results(batch_id, sku, series, month, limit)

Place this file in: client_code/forecast_batch_results/__init__.py
"""

from ._anvil_designer import forecast_batch_resultsTemplate
from anvil import *
import anvil.server


MONTH_NAMES = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]


class forecast_batch_results(forecast_batch_resultsTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'forecast_batch_results'

    # State
    self.current_batch = None

    # Wire header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)

    # Wire filters
    self.dd_batch.set_event_handler('change', self.dd_batch_change)
    self.tb_sku_filter.set_event_handler('pressed_enter', self.filter_changed)
    self.dd_series_filter.set_event_handler('change', self.filter_changed)
    self.dd_month_filter.set_event_handler('change', self.filter_changed)
    self.btn_clear_filters.set_event_handler('click', self.btn_clear_filters_click)

    
    # Month dropdown is static
    month_items = [('All months', None)] + [
      (f'{i} — {MONTH_NAMES[i - 1]}', i) for i in range(1, 13)
    ]
    self.dd_month_filter.items = month_items

    # Initial population
    self.populate_batches()

  # =========================================================================
  # SETUP
  # =========================================================================

  def populate_batches(self):
    """Fill the batch dropdown and trigger the first load."""
    try:
      batches = anvil.server.call('get_recent_batches', limit=25)
    except Exception as e:
      alert(f'Could not load batches: {e}', title='Load Error')
      return

    if not batches:
      self.dd_batch.items = [('No batches available', None)]
      self.lbl_record_count.text = 'No batches'
      return

    # Anvil DropDown items: list of (label, value)
    self.dd_batch.items = batches
    self.dd_batch.selected_value = batches[0][1]  # most recent batch
    self.current_batch = batches[0][1]

    self.populate_series_for_batch()
    self.load_results()

  def populate_series_for_batch(self):
    """Load the series list for the currently selected batch."""
    self.dd_series_filter.items = [('All series', 'All')]

    if not self.current_batch:
      return

    try:
      result = anvil.server.call('get_batch_series_list', self.current_batch)
    except Exception as e:
      self.lbl_header_year.text = f'Error loading series: {e}'
      return

    if result.get('status') == 'success':
      series = result.get('series', [])
      self.dd_series_filter.items = (
        [('All series', 'All')] + [(s, s) for s in series]
      )
      self.dd_series_filter.selected_value = 'All'

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_results(self):
    """Fetch filtered results and populate the grid."""
    if not self.current_batch:
      self.rp_results.items = []
      self.lbl_record_count.text = 'Select a batch to view results'
      return

    sku_filter = self.tb_sku_filter.text or ''
    sku_filter = sku_filter.strip() or None

    series_filter = self.dd_series_filter.selected_value
    if series_filter == 'All':
      series_filter = None

    month_filter = self.dd_month_filter.selected_value

    try:
      result = anvil.server.call(
        'get_forecast_batch_results',
        self.current_batch,
        sku=sku_filter,
        series=series_filter,
        month=month_filter,
        limit=500,
      )
    except Exception as e:
      alert(f'Failed to load results: {e}', title='Load Error')
      return

    if result.get('status') != 'success':
      alert(result.get('error', 'Unknown error'), title='Load Error')
      return

    rows = result.get('rows', [])
    count = result.get('count', 0)
    target_year = result.get('target_year')
    truncated = result.get('truncated', False)

    self.rp_results.items = rows

    suffix = ' (truncated at 500 — add filters to narrow)' if truncated else ''
    noun = 'result' if count == 1 else 'results'
    self.lbl_record_count.text = f'{count} {noun}{suffix}'

    if target_year:
      self.lbl_header_year.text = f'Target year: {target_year}'

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  def btn_refresh_click(self, **event_args):
    self.populate_batches()

  def dd_batch_change(self, **event_args):
    self.current_batch = self.dd_batch.selected_value
    # Series list is batch-specific, so reload it
    self.populate_series_for_batch()
    self.load_results()

  def filter_changed(self, **event_args):
    self.load_results()

  def btn_clear_filters_click(self, **event_args):
    self.tb_sku_filter.text = ''
    self.dd_series_filter.selected_value = 'All'
    self.dd_month_filter.selected_value = None
    self.load_results()

