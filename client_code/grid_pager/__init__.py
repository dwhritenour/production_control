from ._anvil_designer import grid_pagerTemplate
from anvil import *


class grid_pager(grid_pagerTemplate):
  """
    Reusable DataGrid pager - a CUSTOM COMPONENT (2026-09-09).

    Anvil's DataGrid has no page API, so work_orders_condensed carried its
    own footer (oc_footer: lk_first / lk_prev / lbl_paging / lk_next /
    lk_last) since 2026-06-17. This is that footer as a component any
    form can drop under any DataGrid.

    Form name in this app: grid_pager (Anvil generates grid_pagerTemplate and
    looks for a class with the FORM's name - the class below must stay
    grid_pager). Designer (documents/GridPager_Component_Specification.md):
      form settings -> "custom component" ON
      property  rows_per_page   Number   default 15
      event     page_change     (fired after every re-render)
      a FlowPanel, align right, containing:
        lk_first  Link     lk_prev  Link     lbl_paging  Label
        lk_next   Link     lk_last  Link
      (font 12, grey #888888, italic - text is set here in code)

    Use from a host form (drop it under the grid; ANY instance name - the
    host forms find it by duck typing, see _find_pager):
      pager.attach(self.grid_work_orders, self.repeating_panel_1)  # once
      pager.items = rows        # after every load or sort (page -> 1)
    The pager sets the RepeatingPanel's items itself. Read
    self.pager.page_items for the rows currently shown, self.pager.page for
    the 0-based page. rows_per_page can be changed at runtime too.
  """

  def __init__(self, **properties):
    self._rows_per_page = 15
    self._items = []
    self._page = 0
    self._grid = None
    self._panel = None
    self.init_components(**properties)
    self.lk_first.text = '« First'
    self.lk_prev.text = '‹ Prev'
    self.lk_next.text = 'Next ›'
    self.lk_last.text = 'Last »'
    self.lk_first.set_event_handler('click', lambda **e: self.goto(0))
    self.lk_prev.set_event_handler('click', lambda **e: self.goto(self._page - 1))
    self.lk_next.set_event_handler('click', lambda **e: self.goto(self._page + 1))
    self.lk_last.set_event_handler('click', lambda **e: self.goto(10 ** 9))
    self._render()

  # ---- designer property --------------------------------------------------
  @property
  def rows_per_page(self):
    return self._rows_per_page

  @rows_per_page.setter
  def rows_per_page(self, value):
    try:
      self._rows_per_page = max(1, int(value or 15))
    except (TypeError, ValueError):
      self._rows_per_page = 15
    self._render()

  # ---- API ------------------------------------------------------------------
  def attach(self, grid=None, panel=None):
    """Bind to a DataGrid (its built-in paging is switched off) and the
    RepeatingPanel inside it whose items this pager will manage."""
    self._grid = grid
    self._panel = panel
    if grid is not None:
      try:
        grid.rows_per_page = 0        # we paginate; the grid shows what it is given
      except Exception:
        pass
    self._render()

  @property
  def items(self):
    return self._items

  @items.setter
  def items(self, value):
    self._items = list(value or [])
    self._page = 0
    self._render()

  @property
  def page(self):
    return self._page

  @property
  def pages(self):
    return max(1, (len(self._items) + self._rows_per_page - 1) // self._rows_per_page)

  @property
  def page_items(self):
    start = self._page * self._rows_per_page
    return self._items[start:start + self._rows_per_page]

  def goto(self, page):
    """Go to a 0-based page; out-of-range values clamp (10**9 == last)."""
    self._page = page
    self._render()

  def refresh(self):
    """Re-render on the current page (e.g. after an in-place row edit)."""
    self._render()

  # ---- rendering ------------------------------------------------------------
  def _render(self):
    if not hasattr(self, 'lbl_paging'):
      return                          # called from a property setter before build
    total = len(self._items)
    self._page = max(0, min(self._page, self.pages - 1))
    start = self._page * self._rows_per_page
    end = min(start + self._rows_per_page, total)
    if self._panel is not None:
      self._panel.items = self._items[start:end]
    first = start + 1 if total else 0
    self.lbl_paging.text = f"Showing {first} to {end} of {total} entries"
    self.lk_first.visible = self.lk_prev.visible = self._page > 0
    self.lk_next.visible = self.lk_last.visible = end < total
    try:
      self.raise_event('page_change')
    except Exception:
      pass                            # event not declared in the designer yet