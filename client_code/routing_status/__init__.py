from ._anvil_designer import routing_statusTemplate
from anvil import *


class routing_status(routing_statusTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
