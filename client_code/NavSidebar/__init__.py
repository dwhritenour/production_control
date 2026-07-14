from ._anvil_designer import NavSidebarTemplate
from anvil import *
import anvil.server


class NavSidebar(NavSidebarTemplate):
  # Maps a page identifier to the nav button that should be highlighted.
  _ACTIVE_BUTTONS = {
    'transfer_recs':          'btn_transfer_form',
    'trip_editor':            'btn_trip_form',
    'driver_deliver':         'btn_deliver',
    'forecast_dashboard':     'btn_forecast_batch',
    'forecast_sku_detail':    'btn_forecasts',
    'forecast_batch_results': 'btn_results',
    'forecast_accuracy':      'btn_accuracy',
    'abc_classification':     'btn_abcclass',
    'minmax_review':          'btn_minmax',
    'work_orders':            'btn_work_orders',
    'work_orders_condensed':   'btn_work_orders_condensed',
    'sales_orders_due':       'btn_sales_orders',
    'system_admin':           'btn_sys_admin_form',
  }

  # One handler per button -> one open_form target. Defined ONLY here.
  _NAV_TARGETS = {
    'btn_transfer_form':  'transfer_recs',
    'btn_trip_form':      'trip_editor',
    'btn_deliver':        'driver_deliver',
    'btn_forecast_batch': 'forecast_dashboard',
    'btn_forecasts':      'forecast_sku_detail',
    'btn_results':        'forecast_batch_results',
    'btn_accuracy':       'forecast_accuracy',
    'btn_abcclass':       'abc_classification',
    'btn_minmax':         'minmax_review',
    'btn_work_orders':    'work_orders',
    'btn_work_orders_condensed':    'work_orders_condensed',
    'btn_sales_orders':   'sales_orders_due',
    'btn_sys_admin_form': 'system_admin',
  }

  def __init__(self, active=None, **properties):
    self.init_components(**properties)
    self._active = active

    # Wire every nav button to its open_form target in one loop.
    for btn_name, target in self._NAV_TARGETS.items():
      btn = getattr(self, btn_name, None)
      if btn is not None:
        btn.set_event_handler('click', self._make_nav_handler(target))

    self._highlight_active()

  def _make_nav_handler(self, target_form):
    def handler(**event_args):
      open_form(target_form)
    return handler

  def _highlight_active(self):
    btn_name = self._ACTIVE_BUTTONS.get(self._active)
    if not btn_name:
      return
    btn = getattr(self, btn_name, None)
    if btn is None:
      return
    # You are already on this page: highlight it and stop it navigating.
    btn.bold = True
    btn.background = '#1565C0'
    btn.foreground = '#FFFFFF'
    btn.enabled = False

  @property
  def active(self):
    return self._active

  @active.setter
  def active(self, value):
    self._active = value
    self._highlight_active()