from ._anvil_designer import RoutingStatusRowTemplate
from anvil import *


class RoutingStatusRow(RoutingStatusRowTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
