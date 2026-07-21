from ._anvil_designer import PartMasterRowTemplate
from anvil import *


class PartMasterRow(PartMasterRowTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
