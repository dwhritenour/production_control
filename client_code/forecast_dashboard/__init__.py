"""
forecast_dashboard - Forecast Batches List
============================================
Read-only overview of all forecast batches stored in Nexus.
User filters by target year and sees: batch ID, status, SKU/series counts,
training window, created date, notes.

No generation UI in this phase — forecasts are still run from the console app.

Server module: forecast_display.py (calls get_forecast_batches)

Place this file in: client_code/forecast_dashboard/__init__.py
"""

from ._anvil_designer import forecast_dashboardTemplate
from anvil import *
import anvil.server


class forecast_dashboard(forecast_dashboardTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'forecast_dashboard'

    # Wire header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)

    # Wire filter
    self.dd_year_filter.set_event_handler('change', self.dd_year_filter_change)

    # Initial load (also populates the year dropdown)
    self.load_batches(refresh_years=True)

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_batches(self, refresh_years=False):
    """Fetch batches from the server and populate the grid."""
    year_filter = self.dd_year_filter.selected_value
    if year_filter == 'All' or year_filter is None:
      year = None
    else:
      try:
        year = int(year_filter)
      except (ValueError, TypeError):
        year = None

    try:
      result = anvil.server.call('get_forecast_batches', year=year, limit=200)
    except Exception as e:
      alert(f'Failed to load batches: {e}', title='Load Error')
      return

    if result.get('status') != 'success':
      alert(result.get('error', 'Unknown error'), title='Load Error')
      return

    batches = result.get('batches', [])
    self.rp_batches.items = batches

    count = result.get('count', 0)
    self.lbl_record_count.text = f'{count} batch' + ('' if count == 1 else 'es')

    if refresh_years:
      self.populate_year_dropdown(result.get('years', []))

  def populate_year_dropdown(self, years):
    """Populate the year filter dropdown from available target_years."""
    items = [('All years', 'All')] + [(str(y), y) for y in years]
    current = self.dd_year_filter.selected_value
    self.dd_year_filter.items = items
    # Preserve previous selection if still valid, else default to 'All'
    valid_values = [v for _, v in items]
    if current in valid_values:
      self.dd_year_filter.selected_value = current
    else:
      self.dd_year_filter.selected_value = 'All'

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  def btn_refresh_click(self, **event_args):
    self.load_batches(refresh_years=True)

  def dd_year_filter_change(self, **event_args):
    self.load_batches(refresh_years=False)
