from ._anvil_designer import DetialRowTemplateTemplate
from anvil import *
import anvil.server

class DetialRowTemplate(DetialRowTemplateTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Any code you write here will run before the form opens.
