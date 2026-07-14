"""
RowTemplate1 - Transfer Recommendations Row Template  (v5.17 candidate)
======================================================================
Client-side code for individual rows in the DataGrid.

Existing styling (unchanged):
- txt_sku amber/red background for Paused status (amber < 3 days, red > 3 days)
- txt_sku dark-orange + bold + reasoning amber-tinted for has_duplicate_warning

v5.17 (NEW, 2026-05-13): Time-urgency highlight for sales orders within 2 days.
- When time_urgency is 'CRITICAL' (overdue or shipping today) or 'HIGH'
  (within 2 days), txt_time_urgency gets dark-red bold text.
- Designed to compose cleanly with both Paused-status (different cell) and
  duplicate-warning (different cell) styling.
- Uses bold weight as the PRIMARY signal so the urgency cue does not depend
  on color perception alone — useful for users with red-green colorblindness.
  Color is secondary reinforcement, not the only signal.
- If dark red is not distinguishable, swap the color (alternatives listed
  at the v5.17 block below) — bold weight alone will still convey urgency.

Place this file in: client_code/transfer_recs/RowTemplate1/__init__.py
"""

from ._anvil_designer import RowTemplate1Template
from anvil import *
import anvil.server


class RowTemplate1(RowTemplate1Template):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings
    self.init_components(**properties)

    # -----------------------------------------------------------------
    # Color-code SKU based on status
    # -----------------------------------------------------------------
    status = self.item.get('status')
    days_paused = self.item.get('days_paused')

    if status == 'Paused':
      if days_paused and days_paused > 3:
        # Stale paused item - light red
        self.txt_sku.background = '#FFCCCB'
      else:
        # Recently paused - light amber
        self.txt_sku.background = '#FFF3CD'

    # -----------------------------------------------------------------
    # v5.5: Duplicate-warning highlight
    # Tint the reasoning TextArea amber and emphasize the SKU so the
    # POSSIBLE DUPLICATE text at the top of the reasoning is impossible
    # to miss. Runs independently of (and after) the Paused branch so it
    # composes cleanly when both conditions apply.
    # -----------------------------------------------------------------
    if self.item.get('has_duplicate_warning'):
      self.ta_ai_reason.background = '#FFF3CD'   # light amber
      self.ta_ai_reason.foreground = '#664D03'   # dark amber for contrast
      self.txt_sku.foreground = '#CC6600'        # dark orange
      self.txt_sku.bold = True

    # -----------------------------------------------------------------
    # v5.17: Time-urgency highlight (within 2 days)
    # CRITICAL = shipping today or overdue. HIGH = within 2 days.
    # Both get bold + dark-red text on txt_time_urgency so the Hub
    # Manager can scan the page for urgent rows at a glance.
    #
    # Composes with Paused (txt_sku background) and duplicate warning
    # (txt_sku foreground + ta_ai_reason background) because all three
    # target different cells.
    #
    # Color choice rationale: bold weight is the primary accessibility
    # signal; dark red is a secondary reinforcement. If '#C62828' is
    # not visually distinguishable for you, try these alternatives —
    # bold weight still does the job in any of them:
    #   - '#B71C1C'  Material Red 900 (darker, more saturated)
    #   - '#1565C0'  Material Blue 800 (good for red-green colorblindness)
    #   - '#4A148C'  Material Purple 900 (very distinct from existing palette)
    # Or add a background tint as well, e.g.:
    #   self.txt_time_urgency.background = '#FFEBEE'   # very light pink
    # -----------------------------------------------------------------
    time_urg = self.item.get('time_urgency')
    if time_urg in ('CRITICAL', 'HIGH'):
      self.txt_time_urgency.foreground = '#C62828'   # Material Red 800
      self.txt_time_urgency.bold = True

      # -----------------------------------------------------------------
      # Initialize dropdown based on current status
      # -----------------------------------------------------------------
    if status == 'Approved':
      self.dd_decision.selected_value = 'Approve'
    elif status == 'Rejected':
      self.dd_decision.selected_value = 'Reject'
    elif status == 'Paused':
      self.dd_decision.selected_value = 'Pause'
    else:
      self.dd_decision.selected_value = None  # Pending - no selection

      # -----------------------------------------------------------------
      # Initialize transfer quantity field
      # Show manager_override_qty if it exists, otherwise ai_recommended_qty
      # -----------------------------------------------------------------
    override_qty = self.item.get('manager_override_qty')
    if override_qty:
      display_qty = override_qty
    else:
      display_qty = self.item.get('ai_recommended_qty') or self.item.get('calculated_transfer_qty')
    if display_qty is not None:
      self.trans_qty.text = str(int(display_qty))

    # =====================================================================
    # EVENT HANDLERS
    # =====================================================================

  @handle("dd_decision", "change")
  def dd_decision_change(self, **event_args):
    """
        When the user selects a decision from the dropdown,
        store it in self.item so the parent form can collect it.
        """
    selected = self.dd_decision.selected_value

    if selected == 'Approve':
      self.item['decision'] = 'Approved'
      self.item['pause_reason'] = None
    elif selected == 'Reject':
      self.item['decision'] = 'Rejected'
      self.item['pause_reason'] = None
    elif selected == 'Pause':
      self.item['decision'] = 'Paused'
      # reason = anvil.js.window.prompt("Why are you pausing this transfer?", "")
      # self.item['pause_reason'] = reason if reason else None
    else:
      self.item['decision'] = None
      self.item['pause_reason'] = None

  @handle("trans_qty", "lost_focus")
  def trans_qty_lost_focus(self, **event_args):
    """
        When the user edits the transfer quantity,
        store the override value in self.item.
        """
    try:
      qty = int(self.trans_qty.text) if self.trans_qty.text else None
      self.item['transfer_qty_override'] = qty
    except ValueError:
      self.item['transfer_qty_override'] = None