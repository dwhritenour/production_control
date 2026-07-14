from ._anvil_designer import SummaryRowTemplateTemplate
from anvil import *
import anvil.server


class SummaryRowTemplate(SummaryRowTemplateTemplate):
  def __init__(self, **properties):
    self.init_components(**properties)

    if self.item:
      # Populate labels
      self.lbl_trip_date.text = str(self.item.get('trip_date', 'No Date'))
      self.lbl_trip_number.text = str(self.item.get('trip_number', 1))
      self.lbl_sku_count.text = str(self.item.get('sku_count', 0))
      self.lbl_volume.text = self.item.get('volume_display', '0.0')
      self.lbl_weight.text = self.item.get('weight_display', '0.0')
      self.lbl_vol_pct.text = self.item.get('vol_pct', '0%')
      self.lbl_wt_pct.text = self.item.get('wt_pct', '0%')

      # Color-code percentage cells
      vol_pct_num = self.item.get('vol_pct_num', 0)
      wt_pct_num = self.item.get('wt_pct_num', 0)

      # Volume percentage color
      if vol_pct_num > 100:
        self.lbl_vol_pct.foreground = '#D32F2F'  # Red
      elif vol_pct_num >= 90:
        self.lbl_vol_pct.foreground = '#F57C00'  # Orange
      else:
        self.lbl_vol_pct.foreground = '#388E3C'  # Green

        # Weight percentage color
      if wt_pct_num > 100:
        self.lbl_wt_pct.foreground = '#D32F2F'  # Red
      elif wt_pct_num >= 90:
        self.lbl_wt_pct.foreground = '#F57C00'  # Orange
      else:
        self.lbl_wt_pct.foreground = '#388E3C'  # Green
