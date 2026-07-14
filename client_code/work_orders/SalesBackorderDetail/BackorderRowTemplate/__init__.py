from ._anvil_designer import BackorderRowTemplateTemplate
from anvil import *
import anvil.server


class BackorderRowTemplate(BackorderRowTemplateTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    # Color code the status
    status = self.item.get('item_status_display', '')
    self.lbl_status.text = status
    if status == 'Open':
      self.lbl_status.foreground = '#4CAF50'
      self.lbl_status.bold = True
    elif status == 'Closed/Shipped':
      self.lbl_status.foreground = '#7B2FBE'
      self.lbl_status.bold = True
