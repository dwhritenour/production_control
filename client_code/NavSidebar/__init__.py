from ._anvil_designer import NavSidebarTemplate
from anvil import *
import anvil.server
import anvil.js


class NavSidebar(NavSidebarTemplate):
  # Maps a page identifier to the nav button that should be highlighted.
  _ACTIVE_BUTTONS = {
    'work_orders':            'btn_work_orders',
    'work_orders_condensed':   'btn_work_orders_condensed',
    'sales_orders_due':       'btn_sales_orders',
    'sku_details':            'btn_routing_library',
    'part_master_manager':    'btn_part_master',
    'exceptions':             'btn_exceptions',     # work_orders in exception mode
  }

  # One handler per button -> one open_form target. Defined ONLY here.
  _NAV_TARGETS = {
    'btn_work_orders':    'work_orders',
    'btn_work_orders_condensed':    'work_orders_condensed',
    'btn_sales_orders':   'sales_orders_due',
    'btn_routing_library': 'sku_details',
    'btn_part_master':     'part_master_manager',
    # Exceptions (2026-11-05): the Work Orders grid opened on the e21 lines
    # on Hold - same grid, same row template, held lines only. A target may
    # be (form name, kwargs) as well as a bare form name.
    'btn_exceptions':      ('work_orders', {'so_status': 'Hold', 'exception_mode': True}),
  }

  # External apps -> open in a NEW TAB. open_form only navigates within
  # THIS app; another developer's published app is its own Anvil session,
  # so the browser handles it and our user keeps their place here.
  _EXTERNAL_TARGETS = {
    'btn_change_log': 'https://titan-change-log.anvil.app/',
    'btn_titan_ai': 'https://titan-chat.anvil.app/',
  }

  def __init__(self, active=None, **properties):
    self.init_components(**properties)
    self._active = active

    # Wire every nav button to its open_form target in one loop.
    for btn_name, target in self._NAV_TARGETS.items():
      btn = getattr(self, btn_name, None)
      if btn is not None:
        btn.set_event_handler('click', self._make_nav_handler(target))

    # Wire external-app buttons (new browser tab).
    for btn_name, url in self._EXTERNAL_TARGETS.items():
      btn = getattr(self, btn_name, None)
      if btn is not None:
        btn.set_event_handler('click', self._make_external_handler(url))

    self._highlight_active()

  def _make_nav_handler(self, target_form):
    def handler(**event_args):
      if isinstance(target_form, tuple):
        open_form(target_form[0], **target_form[1])
      else:
        open_form(target_form)
    return handler

  def _make_external_handler(self, url):
    def handler(**event_args):
      anvil.js.window.open(url, '_blank')
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
