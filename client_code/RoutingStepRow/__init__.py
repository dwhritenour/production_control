from ._anvil_designer import RoutingStepRowTemplate
from anvil import *


class RoutingStepRow(RoutingStepRowTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
