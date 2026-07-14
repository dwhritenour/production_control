from ._anvil_designer import rp_summaryTemplate
from anvil import *
import anvil.server


class rp_summary(rp_summaryTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)

    # Any code you write here will run before the form opens.
