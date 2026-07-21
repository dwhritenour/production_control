from ._anvil_designer import part_master_managerTemplate
from anvil import *


class part_master_manager(part_master_managerTemplate):
    def __init__(self, **properties):
        super().__init__(**properties)
