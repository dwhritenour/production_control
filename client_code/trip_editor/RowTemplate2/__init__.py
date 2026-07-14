# =============================================================================
# trip_editor/RowTemplate2/__init__.py
# -----------------------------------------------------------------------------
# Version:       5.18.0
# Last updated:  2026-06-10
# Changelog (this module only):
#   5.18.0 (2026-06-10) Qty 0 = remove SKU + cancel recommendation.
#                       Use case (CV42LCSM0600 incident, 2026-06-09/10):
#                       pick slip printed but the piece couldn't be
#                       physically located at the Hub; cycle count
#                       zeroed e21, EasyMorph zeroed Nexus. Previously
#                       there was NO way to clear such a SKU — leaving
#                       it unselected at finalize or cancelling the
#                       batch both revert the rec to 'Applied', and the
#                       next optimizer run packs it again, forever.
#                       Now: entering 0 in txt_qty prompts a confirm
#                       dialog, then calls the new v5.18 server
#                       callable remove_assignment_cancel_rec, which
#                       deletes the assignment, recalcs (or deletes)
#                       the trip, and moves the rec to terminal status
#                       'Cancelled'. On success the parent trip_editor
#                       is refreshed via its existing methods
#                       (load_batch_details / load_pending_batches +
#                       clear_batch_display when the batch emptied) —
#                       no trip_editor/__init__.py change required, and
#                       no Anvil form layout change required (txt_qty
#                       already exists).
#   5.15.0 (2026-05-16) Flipped the defensive default on cb_selected
#                       from True to False. The parent trip_editor
#                       v5.15 sets _selected=False explicitly on every
#                       fresh-loaded assignment, but the .get() default
#                       here is the safety net that fires for any rec
#                       missing the key. Updating it keeps both layers
#                       aligned on the new opt-in semantic. Without
#                       this change a row that somehow arrived without
#                       _selected would default checked, contradicting
#                       the master cb_select_all's unchecked state and
#                       producing a confused-looking initial render.
#   5.8.0 (2026-04-23) Added cb_selected checkbox (default checked).
#                      On toggle, updates self.item['_selected'] and
#                      notifies the parent form via on_selection_changed()
#                      so the Trip Summary (Capacity Check) refreshes.
# =============================================================================

__version__       = "5.18.0"
__version_date__  = "2026-06-10"
__version_notes__ = (
  "Qty 0 in txt_qty now removes the SKU from the batch and cancels the "
  "underlying recommendation (terminal 'Cancelled' status) via the new "
  "remove_assignment_cancel_rec server callable. Kills the immortal "
  "zero-Hub-stock rec loop (CV42LCSM0600 incident)."
)

from ._anvil_designer import RowTemplate2Template
from anvil import *
import anvil.server
from datetime import datetime, date, time


class RowTemplate2(RowTemplate2Template):
  def __init__(self, **properties):
    self.init_components(**properties)
    # In __init__, after init_components:
    if not self.item.get('planned_date'):
      self.dp_date.date = date.today()

    if self.item:
      # v5.15: selection state — default UNCHECKED when the row first
      # renders. Hub Manager must explicitly opt in (individual checkbox
      # or master cb_select_all) before Finalize will act. This matches
      # the v5.15 trip_editor parent form which sets _selected=False on
      # every assignment in load_batch_details.
      self.cb_selected.checked = bool(self.item.get('_selected', False))
      # Mirror back into item so the parent form's refresh_trip_summary
      # and _sync_master_checkbox see a consistent '_selected' flag
      # even for rows the user never touched.
      self.item['_selected'] = self.cb_selected.checked

      # Read-only fields
      self.lbl_sku.text = str(self.item.get('sku', ''))
      self.lbl_location.text = str(self.item.get('location_code', '--') or '--')
      self.lbl_hub_oh.text = str(int(self.item.get('hub_on_hand', 0) or 0))
      self.lbl_priority.text = str(self.item.get('priority_code', '') or '')

      # Editable: Quantity
      qty = self.item.get('adjusted_qty') or self.item.get('qty', 0)
      self.txt_qty.text = str(int(qty or 0))

      # Editable: Trip Date
      planned_date = self.item.get('planned_date')
      if planned_date:
        if isinstance(planned_date, str):
          try:
            self.dp_date.date = datetime.strptime(planned_date, '%Y-%m-%d').date()
          except:
            self.dp_date.date = None
        elif isinstance(planned_date, (date, datetime)):
          self.dp_date.date = planned_date if isinstance(planned_date, date) else planned_date.date()
        else:
          self.dp_date.date = None
      else:
        self.dp_date.date = None

        # Editable: Trip Time (dropdown with common times)
      self.dd_time.items = [
        ('06:00', '06:00'),
        ('07:00', '07:00'),
        ('08:00', '08:00'),
        ('09:00', '09:00'),
        ('10:00', '10:00'),
        ('11:00', '11:00'),
        ('12:00', '12:00'),
        ('13:00', '13:00'),
        ('14:00', '14:00'),
        ('15:00', '15:00'),
        ('16:00', '16:00'),
      ]
      planned_time = self.item.get('planned_time')
      if planned_time:
        if isinstance(planned_time, str):
          time_str = planned_time[:5] if len(planned_time) >= 5 else planned_time
          self.dd_time.selected_value = time_str
        elif isinstance(planned_time, time):
          self.dd_time.selected_value = planned_time.strftime('%H:%M')
        else:
          self.dd_time.selected_value = '08:00'
      else:
        self.dd_time.selected_value = '08:00'

        # Editable: Trip Number
      current_trip = self.item.get('trip_number', 1)
      self.dd_trip.items = [(str(i), i) for i in range(1, 11)]
      self.dd_trip.selected_value = current_trip

      # Wire up event handlers
    self.txt_qty.add_event_handler('lost_focus', self.txt_qty_lost_focus)
    self.dp_date.add_event_handler('change', self.dp_date_change)
    self.dd_time.add_event_handler('change', self.dd_time_change)
    self.dd_trip.add_event_handler('change', self.dd_trip_change)
    # v5.8: selection checkbox
    self.cb_selected.add_event_handler('change', self.cb_selected_change)

  def get_parent_form(self):
    """Get the parent trip_editor form to notify of changes."""
    parent = self.parent
    while parent:
      if hasattr(parent, 'on_assignment_changed'):
        return parent
      parent = getattr(parent, 'parent', None)
    return None

  # ---------------------------------------------------------------------
  # v5.8 — selection toggle
  # ---------------------------------------------------------------------
  def cb_selected_change(self, **event_args):
    """Update selection flag and tell the parent to refresh the summary."""
    if not self.item:
      return
    self.item['_selected'] = bool(self.cb_selected.checked)

    parent = self.get_parent_form()
    if parent and hasattr(parent, 'on_selection_changed'):
      parent.on_selection_changed()
    elif parent:
      # Back-compat safety net: fall back to the existing assignment-changed
      # hook so the summary at least re-renders.
      parent.on_assignment_changed()

  def txt_qty_lost_focus(self, **event_args):
    """Handle quantity change - save to database.

    v5.18: qty 0 is no longer a plain qty update — it now means
    "this SKU cannot be fulfilled, remove it and cancel the
    recommendation." See _remove_and_cancel below.
    """
    if not self.item:
      return

    try:
      new_qty = int(self.txt_qty.text or 0)
      old_qty = int(self.item.get('adjusted_qty') or self.item.get('qty', 0))

      if new_qty < 0:
        self.txt_qty.text = str(old_qty)
        alert("Quantity cannot be negative")
        return

      # ── v5.18: qty 0 = remove + cancel ──
      if new_qty == 0:
        if old_qty == 0:
          return
        self._remove_and_cancel(old_qty)
        return

      if new_qty != old_qty:
        result = anvil.server.call(
          'update_assignment_qty',
          self.item['assignment_id'],
          new_qty
        )

        if result['success']:
          self.item['adjusted_qty'] = new_qty
          self.item['volume_ft3'] = result.get('new_volume', self.item.get('volume_ft3'))
          self.item['weight_lb'] = result.get('new_weight', self.item.get('weight_lb'))
          Notification(f"Qty updated to {new_qty}").show()

          parent = self.get_parent_form()
          if parent:
            parent.on_assignment_changed()
        else:
          self.txt_qty.text = str(old_qty)
          alert(f"Error saving: {result.get('error')}")

    except ValueError:
      old_qty = int(self.item.get('adjusted_qty') or self.item.get('qty', 0))
      self.txt_qty.text = str(old_qty)
      alert("Please enter a valid number")

  def _remove_and_cancel(self, old_qty):
    """v5.18 — qty-0 path: confirm, then remove this assignment from the
    batch and cancel the underlying recommendation (terminal status
    'Cancelled', so it can NOT cycle back to 'Applied' via the
    leftover-revert or Cancel Batch paths).

    On success, refreshes the parent trip_editor through its existing
    public methods — no parent code change required:
      * batch still has rows -> load_batch_details(current_batch_id)
      * batch emptied        -> load_pending_batches() + clear_batch_display()
    On cancel/failure, restores the old qty in the textbox.
    """
    sku = self.item.get('sku', '')
    msg = (
      f"Remove {sku} from this batch and CANCEL the transfer "
      f"recommendation?\n\n"
      f"Use this when the SKU cannot be fulfilled (e.g. not found at "
      f"the Hub / zero on hand).\n\n"
      f"The recommendation will NOT return to the optimizer queue. "
      f"If Starlite still needs this SKU once the Hub has stock again, "
      f"a fresh recommendation will be raised automatically.\n\n"
      f"This cannot be undone."
    )
    if not confirm(msg, title="Remove SKU and Cancel Recommendation?"):
      self.txt_qty.text = str(old_qty)
      return

    result = anvil.server.call(
      'remove_assignment_cancel_rec',
      self.item['assignment_id'],
      'Set to qty 0 in Trip Editor'
    )

    if not result.get('success'):
      self.txt_qty.text = str(old_qty)
      alert(f"Error removing SKU: {result.get('error', 'Unknown')}")
      return

    Notification(result.get('message', f"{sku} removed.")).show()

    parent = self.get_parent_form()
    if not parent:
      return
    try:
      if result.get('batch_deleted'):
        # Removal emptied the whole batch — refresh the dropdown and
        # clear the grids (mirrors the delete_batch UX).
        parent.load_pending_batches()
        parent.clear_batch_display()
      else:
        parent.load_batch_details(parent.current_batch_id)
    except AttributeError:
      # Defensive: unexpected parent shape — fall back to the
      # assignment-changed hook so at least the summary re-renders.
      parent.on_assignment_changed()

  def dp_date_change(self, **event_args):
    """Handle trip date change - save to database."""
    if not self.item:
      return

    new_date = self.dp_date.date
    if new_date:
      result = anvil.server.call(
        'update_assignment_date_time',
        self.item['assignment_id'],
        new_date.strftime('%Y-%m-%d'),
        self.dd_time.selected_value
      )

      if result['success']:
        self.item['planned_date'] = new_date.strftime('%Y-%m-%d')
        Notification(f"Date updated").show()

        parent = self.get_parent_form()
        if parent:
          parent.on_assignment_changed()
      else:
        alert(f"Error: {result.get('error')}")

  def dd_time_change(self, **event_args):
    """Handle trip time change - save to database."""
    if not self.item:
      return

    new_time = self.dd_time.selected_value
    current_date = self.dp_date.date

    if new_time and current_date:
      result = anvil.server.call(
        'update_assignment_date_time',
        self.item['assignment_id'],
        current_date.strftime('%Y-%m-%d'),
        new_time
      )

      if result['success']:
        self.item['planned_time'] = new_time
        Notification(f"Time updated").show()
      else:
        alert(f"Error: {result.get('error')}")

  def dd_trip_change(self, **event_args):
    """Handle trip number change - save to database."""
    if not self.item:
      return

    new_trip = self.dd_trip.selected_value
    old_trip = self.item.get('trip_number')

    if new_trip and new_trip != old_trip:
      result = anvil.server.call(
        'update_assignment_trip',
        self.item['assignment_id'],
        new_trip
      )

      if result['success']:
        self.item['trip_number'] = new_trip
        Notification(f"Moved to Trip {new_trip}").show()

        parent = self.get_parent_form()
        if parent:
          parent.on_assignment_changed()
      else:
        self.dd_trip.selected_value = old_trip
        alert(f"Error: {result.get('error')}")