from ._anvil_designer import NavSidebarTemplate
from anvil import *
import anvil.server


class NavSidebar(NavSidebarTemplate):
  # Maps a page identifier to the nav button that should be highlighted.
  _ACTIVE_BUTTONS = {
    'work_orders':            'btn_work_orders',
    'work_orders_condensed':   'btn_work_orders_condensed',
    'sales_orders_due':       'btn_sales_orders',
    'processed_transfers':    'btn_processed',
    'sku_details':            'btn_routing_library',
    'part_master_manager':    'btn_part_master',
  }

  # One handler per button -> one open_form target. Defined ONLY here.
  _NAV_TARGETS = {
    'btn_work_orders':    'work_orders',
    'btn_work_orders_condensed':    'work_orders_condensed',
    'btn_sales_orders':   'sales_orders_due',
    'btn_processed':      'processed_transfers',  # fixed 2026-07-21: was inverted
    'btn_routing_library': 'sku_details',
    'btn_part_master':     'part_master_manager',
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

  @handle("btn_routing_library", "click")
  def btn_routing_library_click(self, **event_args):
    """This method is called when the button is clicked"""
    pass  # Write Code Here

  @handle("btn_part_master", "click")
  def btn_part_master_click(self, **event_args):
    """This method is called when the button is clicked"""
    pass  # Write Code Here
