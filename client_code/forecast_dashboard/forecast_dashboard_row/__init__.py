"""
forecast_dashboard_row - Row Template for the Forecast Dashboard grid
======================================================================
This form is rendered once per row in `rp_batches` on `forecast_dashboard`.

Each instance has `self.item` populated with one batch dict from the server
(get_forecast_batches output). The row displays the batch fields via Labels
bound to data_keys in the parent DataGrid, and adds a per-row "Export"
button that generates the Purchasing Excel for that batch and triggers a
client-side download.

Place this file in: client_code/forecast_dashboard_row/__init__.py

Anvil designer setup (covered in BUILD_FORM_5):
  - Create a Form named `forecast_dashboard_row`
  - Layout: a single DataRowPanel matching the dg_batches columns
  - Add a Label for each existing data_key column (binds via the
    DataGrid column IDs)
  - Add one Button named `btn_export` in a final new "Export" column
"""

from ._anvil_designer import forecast_dashboard_rowTemplate
from anvil import *
import anvil.server
import anvil.media


class forecast_dashboard_row(forecast_dashboard_rowTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)
    # Wire the export button. The button itself sits in the row template;
    # `self.item` is the batch dict for this row.
    self.btn_export.set_event_handler('click', self.btn_export_click)

  # =========================================================================
  # EVENT HANDLERS
  # =========================================================================

  def btn_export_click(self, **event_args):
    """
    Generate the Purchasing Excel for this row's batch and download it.

    The server returns an anvil.BlobMedia. We disable the button while the
    server is working so impatient users don't fire it twice.
    """
    if not self.item or 'batch_id' not in self.item:
      alert('No batch on this row — refresh the dashboard.', title='Export')
      return

    batch_id = self.item['batch_id']

    # Disable button + visual indicator
    original_text = self.btn_export.text
    self.btn_export.enabled = False
    self.btn_export.text = 'Generating...'

    try:
      result = anvil.server.call('get_purchasing_export', batch_id)
    except Exception as e:
      alert(f'Export failed: {e}', title='Export Error')
      return
    finally:
      self.btn_export.enabled = True
      self.btn_export.text = original_text

    # Server returns either a BlobMedia (success) or a dict (error)
    if isinstance(result, dict) and result.get('status') == 'error':
      alert(result.get('error', 'Unknown error'), title='Export Error')
      return

    # BlobMedia → trigger browser download
    try:
      anvil.media.download(result)
    except Exception as e:
      alert(f'Download failed: {e}', title='Export')