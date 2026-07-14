"""
forecast_sku_detail - SKU Forecast Detail Form
================================================
Read-only form for drilling into a single SKU's forecast:
  - Metadata (description, series, classification, model used)
  - Chart: historical sales + forecast line + confidence band + actuals overlay
  - Monthly grid: month-by-month historical, forecast, actual, variance

Server module: forecast_display.py

Place this file in: client_code/forecast_sku_detail/__init__.py
"""

from ._anvil_designer import forecast_sku_detailTemplate
from anvil import *
import anvil.server


class forecast_sku_detail(forecast_sku_detailTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'forecast_sku_detail'

    # State
    self.current_sku = None
    self.current_batch = None

    # Wire header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)

    # Wire search controls
    self.tb_sku_search.set_event_handler('pressed_enter', self.tb_sku_search_pressed_enter)
    self.dd_sku_results.set_event_handler('change', self.dd_sku_results_change)
    self.dd_batch.set_event_handler('change', self.dd_batch_change)

    # Initial setup
    self.populate_batches()
    self.clear_display()

  # =========================================================================
  # INITIAL SETUP
  # =========================================================================

  def populate_batches(self):
    """Load recent batches into the batch dropdown."""
    try:
      batches = anvil.server.call('get_recent_batches', limit=25)
      self.dd_batch.items = [('— Select batch —', None)] + batches
      if batches:
        # Default to most recent batch
        self.dd_batch.selected_value = batches[0][1]
        self.current_batch = batches[0][1]
    except Exception as e:
      alert(f'Could not load batches: {e}', title='Load Error')

  def clear_display(self):
    """Reset all display widgets to empty state."""
    self.lbl_sku.text = '— Enter a SKU above —'
    self.lbl_description.text = ''
    self.lbl_series.text = ''
    self.lbl_classification.text = ''
    self.lbl_model_used.text = ''
    self.lbl_history_months.text = ''
    self.plt_forecast.data = []
    self.plt_forecast.layout = {'title': 'No SKU selected'}
    self.rp_monthly.items = []

  # =========================================================================
  # SEARCH
  # =========================================================================

  def tb_sku_search_pressed_enter(self, **event_args):
    """Run the SKU search and populate the results dropdown."""
    term = (self.tb_sku_search.text or '').strip()
    if not term:
      self.dd_sku_results.items = []
      return

    try:
      results = anvil.server.call('search_skus_for_forecast', term, limit=25)
    except Exception as e:
      alert(f'Search failed: {e}', title='Search Error')
      return

    if not results:
      self.dd_sku_results.items = [('No matches found', None)]
      self.dd_sku_results.selected_value = None
    else:
      self.dd_sku_results.items = [('— Choose a SKU —', None)] + results
      self.dd_sku_results.selected_value = None

  def dd_sku_results_change(self, **event_args):
    """User picked a SKU from the dropdown."""
    sku = self.dd_sku_results.selected_value
    if not sku:
      return
    self.current_sku = sku
    self.load_detail()

  def dd_batch_change(self, **event_args):
    """User picked a different batch."""
    batch = self.dd_batch.selected_value
    if not batch:
      return
    self.current_batch = batch
    if self.current_sku:
      self.load_detail()

  # =========================================================================
  # LOAD & RENDER
  # =========================================================================

  def btn_refresh_click(self, **event_args):
    self.populate_batches()
    if self.current_sku and self.current_batch:
      self.load_detail()

  def load_detail(self):
    """Fetch and render detail for the current SKU + batch."""
    if not self.current_sku or not self.current_batch:
      return

    try:
      result = anvil.server.call(
        'get_forecast_sku_detail',
        self.current_sku,
        self.current_batch,
        history_months=24,
      )
    except Exception as e:
      alert(f'Failed to load detail: {e}', title='Load Error')
      return

    if result.get('status') != 'success':
      alert(result.get('error', 'Unknown error'), title='Load Error')
      return

    self.render(result)

  def render(self, result):
    """Populate the form widgets from the server response."""
    info = result['sku_info']
    batch = result['batch_info']

    # Header info labels
    self.lbl_sku.text = f"{info['sku']}"
    self.lbl_description.text = f"Description: {info.get('description') or '—'}"
    self.lbl_series.text = f"Series: {info.get('series') or '—'}"

    cls = info.get('abc_classification') or '—'
    stocked = 'Yes' if info.get('is_stocked_at_starlite') else 'No'
    self.lbl_classification.text = f"Class: {cls}  |  Stocked at Starlite: {stocked}"

    model = result.get('model_used') or '—'
    self.lbl_model_used.text = f"Model: {model}  |  Target year: {batch.get('target_year')}"

    history_months = result.get('history_months_shown', 0)
    pattern = info.get('demand_pattern') or '—'
    self.lbl_history_months.text = f"History shown: {history_months} months  |  Pattern: {pattern}"

    # Plot
    self.plt_forecast.data = result['plot_data']
    self.plt_forecast.layout = result['plot_layout']

    # Monthly grid
    self.rp_monthly.items = result['monthly']
