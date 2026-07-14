from ._anvil_designer import SalesBackorderDetailTemplate
from anvil import *
import anvil.server


class SalesBackorderDetail(SalesBackorderDetailTemplate):
  def __init__(self, sales_order_number, **properties):
    """Popup form to display sales backorder detail.

    Args:
      sales_order_number: str — the SO number (without release)
    """
    self.init_components(**properties)
    self._sales_order_number = sales_order_number
    self._populate()

  def _populate(self):
    """Fetch and display backorder data."""
    so_num = self._sales_order_number

    self.lbl_header.text = f"Sales Backorder Detail — SO {so_num}"

    result = anvil.server.call('get_sales_backorder_detail', so_num)

    if result['status'] == 'success':
      backorders = result['backorders']
      # Carrier / MOS header (Carrier+MOS feature). Guarded so the form loads
      # before lbl_carrier / lbl_mos are added in the designer.
      carrier = result.get('carrier', '') or '—'
      mos = result.get('mos', '') or '—'
      if hasattr(self, 'lbl_carrier'):
        self.lbl_carrier.text = f"Carrier: {carrier}"
      if hasattr(self, 'lbl_mos'):
        self.lbl_mos.text = f"MOS: {mos}"
      if backorders:
        self.rp_backorders.items = backorders
      else:
        self.lbl_header.text = f"Sales Backorder Detail — SO {so_num}  |  No backorder records found"
    else:
      alert(f"Error loading backorder detail: {result.get('message', 'Unknown error')}")
