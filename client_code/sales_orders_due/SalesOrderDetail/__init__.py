"""
Anvil Popup Form - SalesOrderDetail
===================================
Popup detail view for a single sales order line. Opened from the
Sales Orders Due grid by clicking the row's Details link. Displayed
via alert(content=detail_form, ...) following the WorkOrderDetail
pattern.

Read-only — no actions on this form modify any e21 data.

Author: Dave Whritenour / Claude AI Assistant
Project: Titan Flow Control - Year of Automation
Created: May 2026
"""

from ._anvil_designer import SalesOrderDetailTemplate
from anvil import *
import anvil.server


class SalesOrderDetail(SalesOrderDetailTemplate):

  def __init__(self, line, **properties):
    """
    Args:
        line: dict — the line detail returned by the server callable
              get_sales_order_line_detail. Contains all sod.* columns
              plus joined inventory, work_order, and backorder fields.
    """
    self.init_components(**properties)
    self._line = line or {}
    self._populate()

    # The form has an inline "Hide" link, but the alert() popup that
    # contains us provides its own Close button — hide the link to
    # avoid duplicate close affordances.
    self.link_close_detail.visible = False

  # =========================================================================
  # POPULATE
  # =========================================================================

  def _populate(self):
    """Fill all detail fields from self._line."""
    line = self._line

    # ---- Header summary ---------------------------------------------------
    order_release = line.get('order_release') or (
      f"{line.get('nx_order_numb', '')}-{line.get('nx_rel_numb', '')}"
    )
    item_no  = line.get('item_no') or line.get('nx_item_no', '')
    sku      = line.get('sku')     or line.get('nx_sku', '')
    shipto   = line.get('shipto_code') or line.get('nx_shipto_code', '')
    nonstock = (line.get('nx_part_status') or '').strip() == 'N'
    nonstock_tag = ' (Nonstock)' if nonstock else ''
    self.lbl_so_summary.text = (
      f"SO {order_release} line {item_no}  —  {sku}{nonstock_tag}"
      + (f"  —  {shipto}" if shipto else "")
    )

    # ---- Quantities -------------------------------------------------------
    self.lbl_so_qty_block.text = (
      f"Ord: {line.get('nx_qty_ordered') or 0:.0f}  |  "
      f"Bal Due: {line.get('nx_qty_balance_due') or 0:.0f}  |  "
      f"Committed: {line.get('nx_qty_committed') or 0:.0f}  |  "
      f"Allocated: {line.get('nx_qty_allocated') or 0:.0f}"
    )

    # ---- Dates (with header context) -------------------------------------
    ship_by  = line.get('ship_by_date',   '--')
    hdr_ship = line.get('header_ship_by', '--')
    due      = line.get('due_date',       '--')
    hdr_due  = line.get('header_due',     '--')
    ship_disp = ship_by + (
      f"  (header: {hdr_ship})" if hdr_ship and hdr_ship != ship_by else ""
    )
    due_disp = due + (
      f"  (header: {hdr_due})"  if hdr_due  and hdr_due  != due     else ""
    )
    self.lbl_so_dates_block.text = f"Ship By: {ship_disp}  |  Due: {due_disp}"

    # ---- Inventory --------------------------------------------------------
    self.lbl_so_inv_block.text = (
      f"Starlite OH: {line.get('starlite_on_hand') or 0:.0f}  |  "
      f"Hub OH: {line.get('hub_on_hand') or 0:.0f}  |  "
      f"On PO: {line.get('starlite_on_po') or 0:.0f}"
    )

    # ---- Linked Work Order -----------------------------------------------
    if line.get('work_order_id'):
      self.lbl_so_wo_block.text = (
        f"Work Order: {line.get('work_order_number')}  "
        f"(status: {line.get('wo_status')})"
      )
      self.lbl_so_wo_block.visible = True
    else:
      self.lbl_so_wo_block.visible = False

    # ---- Backorder flag --------------------------------------------------
    if line.get('has_backorder'):
      self.lbl_so_bo_block.text = "Backorder: this SKU has open backorder lines"
      self.lbl_so_bo_block.visible = True
    else:
      self.lbl_so_bo_block.visible = False

    # ---- SKU-level notes -------------------------------------------------
    sku_notes = (line.get('nx_notes') or '').strip()
    if sku_notes:
      self.lbl_so_sku_notes_header.visible = True
      self.rt_so_sku_notes.visible = True
      self.rt_so_sku_notes.content = sku_notes
    else:
      self.lbl_so_sku_notes_header.visible = False
      self.rt_so_sku_notes.visible = False

    # ---- SO-level notes --------------------------------------------------
    order_notes = (line.get('nx_so_notes') or '').strip()
    if order_notes:
      self.lbl_so_order_notes_header.visible = True
      self.rt_so_order_notes.visible = True
      self.rt_so_order_notes.content = order_notes
    else:
      self.lbl_so_order_notes_header.visible = False
      self.rt_so_order_notes.visible = False