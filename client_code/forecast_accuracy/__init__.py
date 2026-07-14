"""
forecast_accuracy - Accuracy Comparison Form
==============================================
Compares a forecast batch vs actual sales in the usage_data table.
Shows summary metrics (WMAPE, bias, outliers), scatter plot of
forecast-vs-actual, detail grid, and series-level rollup.

Server module: forecast_display.py (get_accuracy_comparison)

Place this file in: client_code/forecast_accuracy/__init__.py
"""

from ._anvil_designer import forecast_accuracyTemplate
from anvil import *
import anvil.server


MONTH_NAMES = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]


class forecast_accuracy(forecast_accuracyTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    self.nav.active = 'forecast_accuracy'
    # State
    self.current_batch = None
    self.current_view = 'detail'   # 'detail' or 'series'
    self.cached_detail_rows = []
    self.cached_series_rollup = []

    # Wire header
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)

    # Wire filters
    self.dd_batch.set_event_handler('change', self.filter_changed)
    self.dd_from_month.set_event_handler('change', self.filter_changed)
    self.dd_to_month.set_event_handler('change', self.filter_changed)
    self.dd_series_filter.set_event_handler('change', self.filter_changed)
    self.cb_outliers_only.set_event_handler('change', self.filter_changed)

    # Wire view-toggle buttons
    self.btn_view_detail.set_event_handler('click', self.btn_view_detail_click)
    self.btn_view_series.set_event_handler('click', self.btn_view_series_click)



    # Populate month dropdowns (static)
    month_items = [(f'{i} — {MONTH_NAMES[i - 1]}', i) for i in range(1, 13)]
    self.dd_from_month.items = month_items
    self.dd_to_month.items = month_items
    self.dd_from_month.selected_value = 1
    self.dd_to_month.selected_value = 3

    self.populate_batches()

  # =========================================================================
  # SETUP
  # =========================================================================

  def populate_batches(self):
    try:
      batches = anvil.server.call('get_recent_batches', limit=25)
    except Exception as e:
      alert(f'Could not load batches: {e}', title='Load Error')
      return

    if not batches:
      self.dd_batch.items = [('No batches available', None)]
      return

    self.dd_batch.items = batches
    self.dd_batch.selected_value = batches[0][1]
    self.current_batch = batches[0][1]

    self.populate_series_for_batch()
    self.load_comparison()

  def populate_series_for_batch(self):
    self.dd_series_filter.items = [('All series', 'All')]
    if not self.current_batch:
      return
    try:
      result = anvil.server.call('get_batch_series_list', self.current_batch)
    except Exception:
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

  def load_comparison(self):
    if not self.current_batch:
      return

    series = self.dd_series_filter.selected_value
    outliers_only = self.cb_outliers_only.checked

    try:
      result = anvil.server.call(
        'get_accuracy_comparison',
        self.current_batch,
        from_month=self.dd_from_month.selected_value,
        to_month=self.dd_to_month.selected_value,
        series=series,
        outliers_only=outliers_only,
      )
    except Exception as e:
      alert(f'Failed to load comparison: {e}', title='Load Error')
      return

    if result.get('status') != 'success':
      alert(result.get('error', 'Unknown error'), title='Load Error')
      return

    self.render(result)

  def render(self, result):
    # ---- Summary strip ----
    s = result.get('summary', {})
    self.lbl_year.text = f"Target: {s.get('target_year', '—')}  |  Months: {s.get('evaluated_months', '—')}"
    self.lbl_sku_count.text = f"SKU-months: {s.get('sku_month_count', 0):,}"
    self.lbl_outliers.text = f"Outliers: {s.get('outlier_count', 0):,}"
    self.lbl_total_forecast.text = f"Total forecast: {s.get('total_forecast') or '—'}"
    self.lbl_total_actual.text = f"Total actual: {s.get('total_actual') or '—'}"
    self.lbl_bias.text = f"Bias: {s.get('bias') or '—'}"
    self.lbl_mape.text = f"MAPE: {s.get('mape') or '—'}%"
    self.lbl_wmape.text = f"WMAPE: {s.get('wmape') or '—'}%"
    self.lbl_wmape_no_out.text = f"WMAPE (no outliers): {s.get('wmape_no_outliers') or '—'}%"

    # ---- Scatter plot ----
    sd = result.get('scatter_data', {})
    self.plt_scatter.data = sd.get('plot_data', [])
    self.plt_scatter.layout = sd.get('plot_layout', {})

    # ---- Render grids (both kept loaded; toggle via visibility) ----
    self.rp_detail.items = result.get('detail_rows', [])
    self.rp_series.items = result.get('series_rollup', [])
    self.show_current_view()

  # =========================================================================
  # VIEW TOGGLE (detail rows vs series rollup)
  # =========================================================================

  def show_current_view(self):
    if self.current_view == 'series':
      self.dg_detail.visible = False
      self.dg_series.visible = True
      count = len(self.rp_series.items or [])
      self.lbl_view_label.text = f'Series Rollup — {count} series (worst WMAPE first)'
      self.btn_view_detail.role = 'tonal-button'
      self.btn_view_series.role = 'elevated-button'
    else:
      self.dg_detail.visible = True
      self.dg_series.visible = False
      count = len(self.rp_detail.items or [])
      self.lbl_view_label.text = f'Detail Rows — {count} row{"" if count == 1 else "s"}'
      self.btn_view_detail.role = 'elevated-button'
      self.btn_view_series.role = 'tonal-button'

  def btn_view_detail_click(self, **event_args):
    self.current_view = 'detail'
    self.show_current_view()

  def btn_view_series_click(self, **event_args):
    self.current_view = 'series'
    self.show_current_view()

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  def btn_refresh_click(self, **event_args):
    self.populate_batches()

  def filter_changed(self, **event_args):
    # When month range or batch changes, series dropdown may go stale
    if event_args.get('sender') is self.dd_batch:
      self.current_batch = self.dd_batch.selected_value
      self.populate_series_for_batch()
    self.load_comparison()