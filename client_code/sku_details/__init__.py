from ._anvil_designer import sku_detailsTemplate
from anvil import *


class sku_details(sku_detailsTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
