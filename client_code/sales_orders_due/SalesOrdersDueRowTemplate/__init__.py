"""
Anvil Row Template - SalesOrdersDueRowTemplate
==============================================
Row template for the Sales Orders Due grid. Renders one open sales order
line. Read-only — no edit affordances. All conditional formatting is
applied here in __init__ based on fields the server already computed
(days_to_ship, ship_date_diverged, ship_by_stale, etc.), plus
band_index assigned client-side by the parent form for per-order
visual grouping.

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: May 2026

Version: v5.17.0 (2026-06-19)
  - Rush ship-method indicator on the Order # cell. When item['is_rush']
    is set (computed server-side from the carrier+method whitelist), a
    trailing lightning glyph (⚡) is appended to the end of the Order #
    cell (txt_order_release), with a tooltip naming the carrier and method.
    This mirrors the existing Ship By ⚑ (diverged) / ⏱ (stale) pattern
    exactly — glyph-at-end only, no bold or recolor. The rush COLOR signal
    lives on the lk_rush_count summary card (form module), not in the grid.
  - The Carrier and MOS grid columns need no code here — txt_carrier /
    txt_mos are filled by data binding to item['nx_carrier'] /
    item['nx_mos']. Requires server v5.21.0+ and form v5.18.0+.

Version: v5.16.0 (2026-05-19)
  - Per-order-release row banding. Reads item['band_index'] (0 or 1,
    assigned by the parent form's _apply_order_grouping_bands helper)
    and sets the DataRowPanel background accordingly. Two soft tints
    so the bands are visible but don't fight the per-cell foreground
    coloring.
  - No designer-side changes required.

Version: v5.15.0 (2026-05-19)
  - Second Ship By cell glyph (⏱) for the ship_by_stale flag, alongside
    the existing ⚑ for header/detail divergence. Glyph + tooltip
    composition restructured so multiple flags coexist.
"""

from ._anvil_designer import SalesOrdersDueRowTemplateTemplate
from anvil import *
import anvil.server


# ---- Color palette (matches WorkOrders / MinMax conventions) -----------------
COLOR_RED      = '#D32F2F'
COLOR_AMBER    = '#FF9800'
COLOR_YELLOW   = '#FFB300'
COLOR_GREEN    = '#388E3C'
COLOR_BLUE     = '#2196F3'
COLOR_PURPLE   = '#7B2FBE'
COLOR_SLATE    = '#455A64'
COLOR_GRAY     = '#888888'
COLOR_BLUEGRAY = '#607D8B'

# ---- v5.16.0: per-order-release banding tints -------------------------------
# Subtle on purpose — these are below the cell-level foreground coloring in
# visual weight so the late/allocated/etc. signals stay dominant.
BAND_COLOR_A = '#FFFFFF'   # plain white
BAND_COLOR_B = '#EEF2F7'   # very light cool gray-blue


class SalesOrdersDueRowTemplate(SalesOrdersDueRowTemplateTemplate):

  def __init__(self, **properties):
    self.init_components(**properties)

    item = self.item or {}

    # ---- v5.16.0: per-order-release row background banding ---------
    # band_index is assigned client-side by the parent form after each
    # sort/refresh. Missing values default to band 0 so the form keeps
    # rendering if it's deployed before the form Python catches up.
    band_index = item.get('band_index', 0)
    try:
      self.background = BAND_COLOR_B if int(band_index) == 1 else BAND_COLOR_A
    except (TypeError, ValueError):
      self.background = BAND_COLOR_A

    # ---- Days-to-ship color coding ---------------------------------
    days = item.get('days_to_ship')
    if days is not None:
      try:
        days = int(days)
        if days < 0:
          self.txt_days.foreground = COLOR_RED
          self.txt_days.bold = True
          # Bold the SKU too — high visibility for late lines
          self.txt_sku.bold = True
        elif days == 0:
          self.txt_days.foreground = COLOR_AMBER
          self.txt_days.bold = True
        elif days <= 3:
          self.txt_days.foreground = COLOR_YELLOW
        elif days <= 7:
          self.txt_days.foreground = COLOR_GREEN
      except (TypeError, ValueError):
        pass

    # ---- Status color coding (mirrors WorkOrders convention) -------
    status = item.get('item_status_desc') or ''
    if status == 'Allocated':
      self.txt_status.foreground = COLOR_BLUE
      self.txt_status.bold = True
    elif status == 'Committed':
      self.txt_status.foreground = COLOR_GREEN
      self.txt_status.bold = True
    elif status == 'Entered':
      self.txt_status.foreground = COLOR_BLUEGRAY
    elif status == 'Hold':
      self.txt_status.foreground = COLOR_PURPLE
      self.txt_status.bold = True
    elif status == 'Material Received/Shipped':
      self.txt_status.foreground = COLOR_SLATE

    # ---- Date-anomaly glyphs on the Ship By cell -------------------
    # Two independent flags can fire on the same line; we compose
    # glyphs + tooltip parts so both surface clearly.
    #   ⚑  ship_date_diverged — header date <> detail date
    #   ⏱  ship_by_stale      — ship-by < today AND due >= today
    glyphs = []
    tooltip_parts = []

    if item.get('ship_date_diverged'):
      glyphs.append('⚑')
      hdr_ship = item.get('header_ship_by') or '--'
      hdr_due  = item.get('header_due') or '--'
      tooltip_parts.append(
        f"Header ship-by: {hdr_ship}  /  header due: {hdr_due}"
      )

    if item.get('ship_by_stale'):
      glyphs.append('⏱')
      tooltip_parts.append(
        "Ship-by has passed but due is still in the future — "
        "ship-by likely needs updating in e21"
      )

    if glyphs:
      ship_text = self.txt_ship_by.text or ''
      suffix = ' ' + ' '.join(glyphs)
      if not ship_text.endswith(suffix):
        self.txt_ship_by.text = ship_text + suffix
      self.txt_ship_by.tooltip = '  |  '.join(tooltip_parts)

    # ---- Rush ship-method indicator on the Order # cell (v5.17.0) ---
    # Mirrors the Ship By ⚑/⏱ pattern exactly: append a glyph to the END of
    # the cell text and set a tooltip. No bold/recolor here on purpose — the
    # COLOR signal for rush lives on the lk_rush_count summary card, keeping
    # the grid cell as clean as the diverged-flag pattern it copies.
    if item.get('is_rush'):
      order_text = self.txt_order_release.text or ''
      rush_glyph = ' ⚡'
      if not order_text.endswith(rush_glyph):
        self.txt_order_release.text = order_text + rush_glyph
      carr = (item.get('nx_carrier') or '').strip()
      mos  = (item.get('nx_mos') or '').strip()
      label = ' '.join(p for p in (carr, mos) if p)
      self.txt_order_release.tooltip = (
        f"Rush shipment — {label}" if label else "Rush shipment"
      )

    # ---- On-hand indicator -----------------------------------------
    bal = item.get('qty_balance_due') or 0
    oh = item.get('starlite_on_hand')
    if oh is None:
      pass  # leave default styling
    elif bal > 0 and oh >= bal:
      self.txt_starlite_oh.foreground = COLOR_GREEN
      self.txt_starlite_oh.bold = True
    elif oh > 0:
      self.txt_starlite_oh.foreground = COLOR_AMBER
    else:
      self.txt_starlite_oh.foreground = COLOR_RED
      self.txt_starlite_oh.bold = True

    # ---- Nonstock part indicator -----------------------------------
    if (item.get('nx_part_status') or '').strip() == 'N':
      if not (item.get('description') or '').strip():
        self.txt_description.text = '(Nonstock)'
      self.txt_description.foreground = COLOR_GRAY
      self.txt_description.italic = True
      self.txt_sku.italic = True

    # ---- Linked Work Order ----------------------------------------
    if item.get('wo_id'):
      self.link_wo.visible = True
      self.link_wo.text = item.get('wo_number') or 'WO'
      self.link_wo.set_event_handler('click', self.link_wo_click)
      self.link_wo.tooltip = (
        f"Work Order {item.get('wo_number')} — status: {item.get('wo_status')}"
      )
    else:
      self.link_wo.visible = False

    # ---- Details link (always visible) ----------------------------
    self.link_details.set_event_handler('click', self.link_details_click)

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  def link_details_click(self, **event_args):
    """Fetch full detail and show as a popup (SalesOrderDetail subform)."""
    result = anvil.server.call(
      'get_sales_order_line_detail',
      self.item.get('order_numb'),
      self.item.get('rel_numb'),
      self.item.get('item_no'),
    )
    if result['status'] != 'success':
      alert(f"Could not load detail: {result.get('message', 'Unknown error')}")
      return

    from ..SalesOrderDetail import SalesOrderDetail
    detail_form = SalesOrderDetail(line=result['line'])
    alert(
      content=detail_form,
      title="",
      large=True,
      buttons=[("Close", None)],
    )

  def link_wo_click(self, **event_args):
    """Open the Work Orders form filtered to this WO."""
    open_form('work_orders', search_text=self.item.get('wo_number'))