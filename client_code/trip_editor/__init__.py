# =============================================================================
# trip_editor/__init__.py
# -----------------------------------------------------------------------------
# Version:       5.19.0
# Last updated:  2026-06-10
# Changelog (this module only):
#   5.19.0 (2026-06-10) Clear Zero-Stock SKUs button (btn_clear_zero_stock):
#     * NEW: button in the optimizer action bar (next to btn_run_optimizer,
#       see Trip_Editor_ClearZeroStock_Form_Specification.docx). Clicking it
#       calls the v5.19 server callable cancel_zero_stock_recs, which wraps
#       the new Nexus stored procedure dbo.sp_cancel_zero_hub_stock_recs.
#     * Flow: dry_run preview -> confirm dialog listing each SKU/qty ->
#       execute -> status message + refresh.
#     * Why: the v5.18 pack-time guard skips Applied/Auto-Approved recs
#       with zero live Hub availability, so they never enter a batch and
#       the per-row qty-0 path can't reach them. They sit 'Applied'
#       forever, repeat in the "Skipped N SKU(s)" optimizer message, and
#       keep crediting starlite_addition_active (phantom inbound). This
#       button is the Hub Manager's terminal-cancel for that population.
#     * Safe by design: detection ignores zero-balance Hub SKUs, so a
#       fresh rec is raised automatically when stock returns. Cancelling
#       never loses demand.
#     * Defensive against pre-deploy YAML: handler uses the @handle
#       decorator, so a form without the button simply never fires it.
#   5.15.0 (2026-05-16) Master select-all checkbox + opt-in default:
#     * NEW: cb_select_all CheckBox in the header bar (placed next to
#       dd_sort per the v5.15 Form Specification). Two-state auto-sync:
#         - Clicking the master forces every assignment's _selected to
#           match its new value; the items grid and trip summary refresh.
#         - on_selection_changed (called when a row checkbox toggles)
#           now also recomputes the master state: checked iff every
#           assignment is selected, otherwise unchecked. So flipping
#           one row off when all were checked clears the master too.
#         - Dynamic label via _update_master_label(): the checkbox text
#           reads "Select All SKUs" when unchecked and "Deselect All
#           SKUs" when checked, so the label describes the action a
#           click WILL perform rather than the current state. Updated
#           from every place that mutates cb_select_all.checked so the
#           text never drifts out of sync.
#     * DEFAULT CHANGE: load_batch_details now sets _selected=False on
#       every assignment when a batch first loads. Hub Manager must
#       explicitly opt in via either individual row checkboxes or the
#       master cb_select_all before clicking Finalize. Companion
#       update in RowTemplate2 v5.15 changes its defensive .get()
#       default from True to False so the two layers agree on the
#       opt-in semantic.
#     * The btn_finalize_click "zero selected" guard from v5.8 now
#       fires routinely on a fresh batch (since nothing is preselected).
#       That's the intent: the Hub Manager's selection has to be a
#       deliberate action, not an accidental whole-batch send.
#   5.14.0 (2026-05-16) Sort dropdown on the items grid (dd_sort):
#     * Three modes: SKU (default), Priority, Trip #.
#     * Tiebreakers per 2026-05-16 design:
#         SKU sort    -> tied SKUs ordered by trip_number ascending
#                        (so duplicate-SKU rows across trips group
#                        together, lowest trip number first).
#         Priority    -> tied priority_sort ordered by SKU ascending.
#         Trip #      -> within each trip, ordered by priority_sort
#                        then SKU ascending.
#     * Sort selection persists across batch loads via
#       self.current_sort_key. Default 'SKU' on form open.
#     * Defensive against pre-deploy YAML: if dd_sort isn't present,
#       the form falls back to whatever order the server returned
#       (planned_date, trip_number, priority_sort, sku) and skips the
#       sort step rather than crashing.
#     * Server-side get_batch_details_flat is unchanged. All sorting
#       is client-side on the already-fetched assignments list, so no
#       extra round trips when the user toggles the dropdown.
#   5.8.0 (2026-04-23) Partial-finalize flow:
#     * load_batch_details initializes _selected=True on every assignment.
#     * refresh_trip_summary filters to the selected subset so the
#       "Trip Summary (Capacity Check)" grid reflects what's actually
#       going on the truck.
#     * New on_selection_changed() hook called by RowTemplate2 checkboxes.
#     * btn_finalize_click now:
#         - aborts if zero selected
#         - delegates to finalize_batch if all selected (old behavior)
#         - otherwise calls finalize_selected_assignments with the
#           dp_carve_date value (defaulting to tomorrow if unset)
#     * Capacity-warning dialog reflects selection totals.
# =============================================================================

__version__       = "5.19.0"
__version_date__  = "2026-06-10"
__version_notes__ = (
  "Clear Zero-Stock SKUs button (btn_clear_zero_stock): preview + confirm "
  "+ execute flow over the new cancel_zero_stock_recs server callable / "
  "dbo.sp_cancel_zero_hub_stock_recs stored procedure. Terminal-cancels "
  "Applied recs with zero live Hub availability that the v5.18 pack guard "
  "keeps out of batches."
)

# v5.14: Allowed sort keys for self.current_sort_key. The dd_sort
# component's items list (set in the Anvil designer per the v5.14 Form
# Specification doc) uses these exact strings as the value side of each
# (label, value) pair so the handler can read selected_value directly.
SORT_KEYS = ('SKU', 'Priority', 'Trip #')
DEFAULT_SORT_KEY = 'SKU'

from ._anvil_designer import trip_editorTemplate
from anvil import *
import anvil.server
from datetime import date, timedelta


class trip_editor(trip_editorTemplate):
  def __init__(self, batch_id=None, **properties):
    self.init_components(**properties)
    self.nav.active = 'trip_editor'

    # Store truck capacity for validation
    self.truck_capacity = {'volume_ft3': 1456.0, 'weight_lb': 13000.0}

    # Store current batch data
    self.current_batch_id = None
    self.assignments = []

    # v5.14: track the user's chosen sort order across batch loads.
    # Initialized to DEFAULT_SORT_KEY so the very first batch that
    # loads is sorted by SKU even before the Hub Manager interacts
    # with dd_sort.
    self.current_sort_key = DEFAULT_SORT_KEY

    # v5.14: mirror the default into the dropdown so the visible
    # selection matches the active sort. Wrapped in try/except so a
    # pre-deploy YAML (no dd_sort yet) doesn't crash the form open.
    try:
      self.dd_sort.selected_value = self.current_sort_key
    except AttributeError:
      pass

    # v5.15: master select-all checkbox starts unchecked. Same defensive
    # try/except so a pre-deploy YAML (no cb_select_all yet) doesn't
    # crash the form open. _update_master_label below sets the initial
    # text to "Select All SKUs" to match the unchecked state.
    try:
      self.cb_select_all.checked = False
    except AttributeError:
      pass
    self._update_master_label()

    # Load pending batches on form open
    self.load_pending_batches()

    # Initialize PDF dropdowns
    self.init_pdf_dropdowns()  # ← ADD THIS

    # v5.8: default the carve DatePicker to tomorrow so the Hub Manager
    # doesn't have to pick a date for the common case. They can override
    # at any time; the value is only read when they click Finalize with
    # a partial selection.
    try:
      self.dp_carve_date.date = date.today() + timedelta(days=1)
    except AttributeError:
      # dp_carve_date not deployed yet (pre-v5.8 YAML) — degrade gracefully.
      pass

    # If a batch_id was passed, select it
    if batch_id:
      self.dd_batch.selected_value = batch_id
      self.load_batch_details(batch_id)


  def init_pdf_dropdowns(self):
    """Initialize the PDF generation date picker and batch dropdown."""
    from datetime import date
    # Default to today's date
    self.dp_date_pdf.date = date.today()

    # Load batches for initial date filter
    self.refresh_batch_dropdown()


  def refresh_batch_dropdown(self):
    """Refresh the batch dropdown based on selected date filter."""
    selected_date = self.dp_date_pdf.date  # Returns None or a date object

    batches = anvil.server.call('get_available_batches', date_filter=selected_date)

    if batches:
      self.dd_batch_pdf.items = [(b['display'], b['batch_id']) for b in batches]
      self.dd_batch_pdf.selected_value = batches[0]['batch_id']
      self.btn_trip_pdf.enabled = True
    else:
      self.dd_batch_pdf.items = [("No batches found for this date", None)]
      self.dd_batch_pdf.selected_value = None
      self.btn_trip_pdf.enabled = False

  def load_pending_batches(self):
    """Load list of proposed batches into dropdown."""
    self.lbl_status.text = "Loading batches..."
    result = anvil.server.call('get_pending_optimizer_batches')

    if result['success']:
      batches = result['batches']
      if batches:
        # Remember any current selection so we can preserve it if still valid
        prior_selection = self.dd_batch.selected_value
        valid_ids = {b['batch_id'] for b in batches}

        self.dd_batch.items = [
          (f"{b['batch_id']} ({b['trip_count']} trips, {b['total_skus']} SKUs)", b['batch_id'])
          for b in batches
        ]

        # Clear selection if the prior value is no longer a valid batch,
        # otherwise Anvil renders "<Invalid value>" in the dropdown.
        if prior_selection not in valid_ids:
          self.dd_batch.selected_value = None

        self.lbl_status.text = f"Found {len(batches)} pending batch(es)"
      else:
        # No batches available — clear items AND the selected value so the
        # dropdown doesn't show "<Invalid value>" from a stale selection.
        self.dd_batch.items = []
        self.dd_batch.selected_value = None
        self.clear_batch_display()
        self.lbl_status.text = "No pending batches"
    else:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  def load_batch_details(self, batch_id):
    """Load full details of selected batch."""
    if not batch_id:
      return

    self.lbl_status.text = "Loading batch details..."
    result = anvil.server.call('get_batch_details_flat', batch_id)

    if result['success']:
      self.current_batch_id = batch_id
      self.truck_capacity = result['truck_capacity']
      self.assignments = result['assignments']

      # v5.15: default every row to UNCHECKED when the batch first loads.
      # The Hub Manager must explicitly opt-in via individual row
      # checkboxes or the master cb_select_all before clicking Finalize.
      # RowTemplate2 v5.15 mirrors this False onto cb_selected via its
      # .get('_selected', False) defensive default.
      for a in self.assignments:
        a['_selected'] = False

      # v5.15: also reset the master checkbox visually. Since all rows
      # are now unchecked, the master should match. Defensive against
      # pre-deploy YAML. _update_master_label keeps the text in sync
      # ("Select All SKUs" on this newly-unchecked state).
      try:
        self.cb_select_all.checked = False
      except AttributeError:
        pass
      self._update_master_label()

      # Update batch header
      self.lbl_batch_header.text = f"Batch: {batch_id}"

      # Calculate and display trip summaries
      self.refresh_trip_summary()

      # v5.14: sort self.assignments per the user's current dropdown
      # choice (defaults to SKU on form open) and load them into the
      # detail grid via _apply_sort_to_grid so the wired order matches
      # the visible order in dd_sort.
      self._apply_sort_to_grid()

      self.lbl_status.text = f"Loaded {len(self.assignments)} assignments"
    else:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  def _selected_assignments(self):
    """v5.8 helper — return the subset of self.assignments currently
    selected (checkbox checked). Defaults to the full list if any row
    predates the '_selected' flag (defensive)."""
    return [a for a in (self.assignments or [])
            if a.get('_selected', True)]

  def _sort_assignments(self, assignments):
    """v5.14 helper — return a list of assignment dicts sorted by
    self.current_sort_key with the agreed tiebreakers.

    SKU       -> (sku ASC, trip_number ASC)
    Priority  -> (priority_sort ASC, sku ASC)
    Trip #    -> (trip_number ASC, priority_sort ASC, sku ASC)

    Sorting is stable in Python, so the secondary keys in each tuple
    serve as proper tiebreakers. The original list is not mutated —
    we always return a fresh sorted copy so callers can rely on
    leaving the input list alone.

    Defensive: any unknown key in self.current_sort_key falls back to
    SKU order. This shouldn't be reachable (dd_sort items are fixed
    in the YAML), but the cost of being defensive is one comparison.
    """
    if not assignments:
      return []

    key_fn = {
      'SKU':       lambda a: (a.get('sku') or '',
                              a.get('trip_number') or 0),
      'Priority':  lambda a: (a.get('priority_sort') or 0,
                              a.get('sku') or ''),
      'Trip #':    lambda a: (a.get('trip_number') or 0,
                              a.get('priority_sort') or 0,
                              a.get('sku') or ''),
    }.get(self.current_sort_key,
          lambda a: (a.get('sku') or '', a.get('trip_number') or 0))

    return sorted(assignments, key=key_fn)

  def _apply_sort_to_grid(self):
    """v5.14 — re-sort self.assignments and push to rp_items.

    Called both from load_batch_details (after fresh data lands) and
    from dd_sort_change (when the user picks a different sort). The
    sort is in-place on self.assignments so that downstream operations
    (e.g. _selected_assignments iterating the list) see the new order.
    """
    self.assignments = self._sort_assignments(self.assignments)
    self.rp_items.items = self.assignments

  def refresh_trip_summary(self):
    """Calculate per-trip-group summaries from assignments.

    v5.8: now computed on the *selected* subset only. When everything is
    checked this matches the v5.7 output exactly. When the Hub Manager
    unchecks rows, the summary — and by extension the capacity warnings
    — reflect the load that will actually go on the truck after the
    carve.
    """
    selected = self._selected_assignments()
    if not selected:
      self.rp_summary.items = []
      return

    # Group by (planned_date, trip_number) — same as v5.7
    groups = {}
    for a in selected:
      key = (a.get('planned_date', 'No Date'), a.get('trip_number', 1))
      if key not in groups:
        groups[key] = {
          'trip_date': key[0] or 'No Date',
          'trip_number': key[1],
          'sku_count': 0,
          'total_volume': 0.0,
          'total_weight': 0.0
        }
      groups[key]['sku_count'] += 1

      # Use adjusted_qty if set, otherwise qty
      qty = float(a.get('adjusted_qty') or a.get('qty') or 0)
      orig_qty = float(a.get('qty') or 1)

      # Calculate unit values from original data
      unit_vol = float(a.get('volume_ft3') or 0) / orig_qty if orig_qty > 0 else 0
      unit_wt = float(a.get('weight_lb') or 0) / orig_qty if orig_qty > 0 else 0

      groups[key]['total_volume'] += qty * unit_vol
      groups[key]['total_weight'] += qty * unit_wt

    # Calculate percentages and format for display
    cap_vol = self.truck_capacity.get('volume_ft3', 1456.0)
    cap_wt = self.truck_capacity.get('weight_lb', 13000.0)

    summaries = []
    for key in sorted(groups.keys(), key=lambda x: (x[0] or '9999-99-99', x[1])):
      g = groups[key]
      vol_pct = (g['total_volume'] / cap_vol * 100) if cap_vol > 0 else 0
      wt_pct = (g['total_weight'] / cap_wt * 100) if cap_wt > 0 else 0

      summaries.append({
        'trip_date': g['trip_date'],
        'trip_number': g['trip_number'],
        'sku_count': g['sku_count'],
        'volume_display': f"{g['total_volume']:,.1f}",
        'weight_display': f"{g['total_weight']:,.1f}",
        'vol_pct': f"{vol_pct:.0f}%",
        'wt_pct': f"{wt_pct:.0f}%",
        'vol_pct_num': vol_pct,
        'wt_pct_num': wt_pct
      })

    self.rp_summary.items = summaries

  def clear_batch_display(self):
    """Clear all batch-related displays."""
    self.current_batch_id = None
    self.assignments = []
    self.lbl_batch_header.text = "Batch: (none selected)"
    self.rp_summary.items = []
    self.rp_items.items = []

    # ===== Event Handlers =====

  @handle("dd_batch", "change")
  def dd_batch_change(self, **event_args):
    """Handle batch selection change."""
    batch_id = self.dd_batch.selected_value
    if batch_id:
      self.load_batch_details(batch_id)
    else:
      self.clear_batch_display()

  @handle("dd_sort", "change")
  def dd_sort_change(self, **event_args):
    """v5.14: re-sort the items grid when the user picks a different
    sort key. Pulls the new key from the dropdown, validates it
    against SORT_KEYS (defensive — should always be one of the
    allowed values since the dropdown items are fixed in the YAML),
    stores it on self.current_sort_key so it persists across the
    next batch load, then re-applies the sort to whatever data is
    currently loaded.

    No server call — the sort is purely client-side on the already-
    fetched self.assignments list, so the response is instant even
    on large batches.
    """
    new_key = self.dd_sort.selected_value
    if new_key not in SORT_KEYS:
      # Defensive: revert the visible selection to whatever was the
      # last valid key. Shouldn't be reachable since dd_sort items
      # are fixed in the YAML, but cheap to guard.
      self.dd_sort.selected_value = self.current_sort_key
      return

    if new_key == self.current_sort_key:
      # No-op: the dropdown fired change but the value matches what
      # we already have. Skip the re-sort to avoid pointless work
      # when the user clicks the same option twice.
      return

    self.current_sort_key = new_key
    if self.assignments:
      self._apply_sort_to_grid()

  @handle("btn_run_optimizer", "click")
  def btn_run_optimizer_click(self, **event_args):
    """Run the truck optimizer to create new trip assignments.

    Option B Consolidation: If there's already an unprocessed (Proposed)
    batch, the server returns a consolidation_needed flag. We prompt the
    user to consolidate or cancel, then re-call with consolidate=True.
    """
    self.lbl_status.text = "Running optimizer..."
    result = anvil.server.call('run_truck_optimizer')

    if result.get('consolidation_needed'):
      # ── Consolidation prompt ──
      summary = result.get('pending_summary', {})
      existing_items = summary.get('total_items', 0)
      new_items = result.get('new_item_count', 0)
      batch_count = summary.get('total_batch_count', 1)

      batch_word = "batch" if batch_count == 1 else "batches"
      message = (
        f"There {'is' if batch_count == 1 else 'are'} already "
        f"{batch_count} pending {batch_word} with {existing_items} items.\n\n"
        f"There are {new_items} new items ready to process.\n\n"
        f"Would you like to consolidate everything and re-optimize "
        f"all {existing_items + new_items} items together?\n\n"
        f"(The existing {batch_word} will be rolled back and a new "
        f"optimized batch will be created.)"
      )

      if confirm(message, title="Consolidate Batches?"):
        # User said YES — consolidate and re-run
        self.lbl_status.text = "Consolidating and re-optimizing..."
        result = anvil.server.call('run_truck_optimizer', consolidate=True)
      else:
        # User said NO — cancel
        self.lbl_status.text = "Optimizer cancelled. Existing batch unchanged."
        return

    # Handle the final result (normal or post-consolidation)
    if result['success']:
      self.load_pending_batches()
      returned_batch_id = result.get('batch_id')
      # Only auto-select the returned batch if it actually shows up in
      # the dropdown. A batch_id for a batch with zero trips won't
      # appear in get_pending_optimizer_batches() and would otherwise
      # cause Anvil to render "<Invalid value>".
      valid_batch_ids = {val for (_label, val) in (self.dd_batch.items or [])}
      if returned_batch_id and returned_batch_id in valid_batch_ids:
        self.dd_batch.selected_value = returned_batch_id
        self.load_batch_details(returned_batch_id)
      else:
        # No new batch produced, or the returned batch has no trips.
        # Reset so we don't display "<Invalid value>".
        self.dd_batch.selected_value = None
        self.clear_batch_display()
      # Set the status message LAST so load_pending_batches() doesn't overwrite it
      self.lbl_status.text = result['message']
    else:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  @handle("btn_consolidate", "click")
  def btn_consolidate_click(self, **event_args):
    """Option A: Manually consolidate all Proposed batches into one.

    Reverts all existing Proposed batch items back to Applied,
    deletes those batch records, then re-runs the optimizer so
    everything gets packed into a single optimized batch.
    """
    # First check how many Proposed batches exist
    result = anvil.server.call('get_pending_optimizer_batches')
    if not result['success']:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"
      return

    batches = result.get('batches', [])
    if len(batches) < 2:
      alert("Consolidation requires at least 2 pending batches. "
            "There's nothing to consolidate right now.")
      return

    total_skus = sum(b.get('total_skus', 0) for b in batches)
    batch_ids = ', '.join(b['batch_id'] for b in batches)

    message = (
      f"This will consolidate {len(batches)} pending batches into one:\n\n"
      f"Batches: {batch_ids}\n"
      f"Total SKUs: {total_skus}\n\n"
      f"All existing batch assignments will be rolled back and "
      f"re-optimized together into a single new batch.\n\n"
      f"Continue?"
    )

    if not confirm(message, title="Consolidate Batches"):
      return

    self.lbl_status.text = "Consolidating batches..."
    result = anvil.server.call('run_truck_optimizer', consolidate=True)

    if result['success']:
      self.load_pending_batches()
      returned_batch_id = result.get('batch_id')
      valid_batch_ids = {val for (_label, val) in (self.dd_batch.items or [])}
      if returned_batch_id and returned_batch_id in valid_batch_ids:
        self.dd_batch.selected_value = returned_batch_id
        self.load_batch_details(returned_batch_id)
      else:
        # Defensive reset — avoids "<Invalid value>" in dd_batch if
        # consolidation didn't produce a visible batch.
        self.dd_batch.selected_value = None
        self.clear_batch_display()
      self.lbl_status.text = result['message']
    else:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  @handle("btn_clear_zero_stock", "click")
  def btn_clear_zero_stock_click(self, **event_args):
    """v5.19 — Clear Zero-Stock SKUs.

    Terminal-cancels every optimizer-queue rec (Applied / Auto-Approved)
    whose LIVE Hub availability is zero. These are the recs the v5.18
    pack-time guard reports as "Skipped N SKU(s) with no live Hub stock"
    on every Run Optimizer click — they never enter a batch, so the
    per-row qty-0 path can't reach them.

    Flow: dry-run preview from the server -> confirm dialog listing each
    SKU and qty -> execute via dbo.sp_cancel_zero_hub_stock_recs ->
    status message. Safe: detection ignores zero-balance Hub SKUs, so a
    fresh rec is raised automatically when stock returns.
    """
    self.lbl_status.text = "Checking for zero-stock SKUs..."
    preview = anvil.server.call('cancel_zero_stock_recs', dry_run=True)

    if not preview.get('success'):
      self.lbl_status.text = f"Error: {preview.get('error', 'Unknown')}"
      return

    recs = preview.get('recs', [])
    if not recs:
      self.lbl_status.text = "No zero-stock SKUs in the optimizer queue."
      alert("No zero-stock SKUs to clear.\n\n"
            "Every recommendation in the optimizer queue currently has "
            "live Hub stock available.")
      return

    sku_lines = "\n".join(
      f"  • {r['sku']}  (qty {r['transfer_qty']}, rec {r['recommendation_id']})"
      for r in recs
    )
    msg = (
      f"Cancel {len(recs)} recommendation(s) with ZERO live Hub stock?\n\n"
      f"{sku_lines}\n\n"
      f"These SKUs cannot be transferred (nothing available at the Hub) "
      f"and are skipped by the optimizer on every run.\n\n"
      f"When the Hub has stock again, a fresh transfer recommendation "
      f"will be created automatically — no demand is lost.\n\n"
      f"This cannot be undone."
    )
    if not confirm(msg, title="Clear Zero-Stock SKUs?"):
      self.lbl_status.text = "Clear zero-stock cancelled — nothing changed."
      return

    self.lbl_status.text = f"Cancelling {len(recs)} zero-stock SKU(s)..."
    result = anvil.server.call('cancel_zero_stock_recs', dry_run=False)

    if not result.get('success'):
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"
      return

    Notification(result.get('message', 'Done.'), style="success").show()
    self.lbl_status.text = result.get('message', 'Zero-stock SKUs cleared.')

  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    """Refresh the batch list and current batch details."""
    self.load_pending_batches()
    batch_id = self.dd_batch.selected_value
    if batch_id:
      self.load_batch_details(batch_id)

  @handle("btn_finalize", "click")
  def btn_finalize_click(self, **event_args):
    """Finalize the selected trips.

    v5.8: if ALL rows are checked, this does exactly what v5.7 did —
    finalize_batch(batch_id). If a SUBSET is checked, we carve those
    SKUs into a new mini-batch at dp_carve_date and finalize that; the
    leftovers get reverted to 'Applied' so the next Run Optimizer
    click re-packs them.
    """
    batch_id = self.dd_batch.selected_value
    if not batch_id:
      self.lbl_status.text = "No batch selected"
      return

    total = len(self.assignments or [])
    selected = self._selected_assignments()
    if not selected:
      alert("No SKUs selected. Check at least one row to finalize.")
      return

    # Over-capacity check — rp_summary already reflects the selection,
    # so this correctly warns about the subset load, not the full batch.
    over_capacity = []
    for s in self.rp_summary.items:
      if s.get('vol_pct_num', 0) > 100 or s.get('wt_pct_num', 0) > 100:
        over_capacity.append(f"Trip {s['trip_number']} on {s['trip_date']}")

    if over_capacity:
      if not confirm(f"WARNING: The following trips exceed truck capacity:\n\n" +
                     "\n".join(over_capacity) +
                     "\n\nProceed anyway?"):
        return

    # ── Branch: all-selected → v5.7 path, subset → v5.8 carve path ──
    if len(selected) == total:
      if not confirm(
        "Finalize all trips in this batch?\n\n"
        "This will generate pick lists and cannot be undone."):
        return

      self.lbl_status.text = "Finalizing..."
      result = anvil.server.call('finalize_batch', batch_id)
      self._handle_finalize_result(result)
      return

    # ── Partial carve path ──
    carve_date = None
    try:
      carve_date = self.dp_carve_date.date
    except AttributeError:
      pass
    if not carve_date:
      alert("Please pick a date in the 'Carve date' field before "
            "finalizing a partial selection.")
      return

    leftover_count = total - len(selected)
    msg = (
      f"Finalize {len(selected)} of {total} SKUs as a new trip on "
      f"{carve_date.strftime('%Y-%m-%d')}?\n\n"
      f"The remaining {leftover_count} SKU(s) will be returned to the "
      f"'Applied' queue so you can re-pack them with 'Run Optimizer'.\n\n"
      f"This cannot be undone."
    )
    if not confirm(msg, title="Carve and Finalize?"):
      return

    self.lbl_status.text = f"Finalizing {len(selected)} selected SKUs..."
    assignment_ids = [int(a['assignment_id']) for a in selected]
    result = anvil.server.call(
      'finalize_selected_assignments',
      batch_id,
      assignment_ids,
      carve_date.strftime('%Y-%m-%d'),
    )
    self._handle_finalize_result(result)

  def _handle_finalize_result(self, result):
    """Shared post-finalize handler — downloads pick lists, refreshes the
    batch list, and surfaces any backend error."""
    if result.get('success'):
      for pl in result.get('pick_lists', []):
        anvil.media.download(pl['media'])

      # v5.8 partial responses include extra context worth surfacing in
      # the status bar; fall back to the generic message for v5.7 flow.
      msg = result.get('message', 'Finalized.')
      self.load_pending_batches()
      self.clear_batch_display()
      self.lbl_status.text = msg
    else:
      self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  @handle("btn_cancel_batch", "click")
  def btn_cancel_batch_click(self, **event_args):
    """Cancel/delete the selected batch."""
    batch_id = self.dd_batch.selected_value
    if not batch_id:
      self.lbl_status.text = "No batch selected"
      return

    if confirm(f"Delete batch {batch_id}?\n\nThis will remove all trip assignments and cannot be undone."):
      self.lbl_status.text = "Deleting..."
      result = anvil.server.call('delete_batch', batch_id)

      if result['success']:
        self.lbl_status.text = "Batch deleted"
        self.load_pending_batches()
        self.clear_batch_display()
      else:
        self.lbl_status.text = f"Error: {result.get('error', 'Unknown')}"

  def on_assignment_changed(self):
    """Called by RowTemplate2 when an assignment is modified."""
    # Refresh the summary to reflect changes
    self.refresh_trip_summary()

  def on_selection_changed(self):
    """v5.8 — called by RowTemplate2.cb_selected_change when the Hub
    Manager toggles a row's checkbox. Re-computes the capacity summary
    so the %-full indicators reflect the subset actually selected.

    v5.15 extension: also auto-syncs the master cb_select_all so its
    visible state reflects the aggregate row state. Master is checked
    iff EVERY assignment is selected; otherwise unchecked. The same
    rule applies for the empty-list case (no rows -> master unchecked)
    to avoid the misleading "all checked" appearance on no data.
    """
    self.refresh_trip_summary()
    self._sync_master_checkbox()

  def _sync_master_checkbox(self):
    """v5.15 helper — recompute master cb_select_all from current row
    selection. Defensive against pre-deploy YAML (no cb_select_all yet).

    Wrapped in try/except so this never crashes a real user action
    even if the master checkbox isn't on the form yet.

    Also updates the master label via _update_master_label() so the
    text matches the new state (checked -> "Deselect All SKUs",
    unchecked -> "Select All SKUs").
    """
    try:
      if not self.assignments:
        self.cb_select_all.checked = False
      else:
        all_checked = all(a.get('_selected', False) for a in self.assignments)
        self.cb_select_all.checked = bool(all_checked)
    except AttributeError:
      pass
    self._update_master_label()

  def _update_master_label(self):
    """v5.15: keep the cb_select_all label in sync with its checked
    state. Checked -> "Deselect All SKUs" (because clicking it will
    deselect). Unchecked -> "Select All SKUs" (because clicking will
    select). Defensive against pre-deploy YAML."""
    try:
      if self.cb_select_all.checked:
        self.cb_select_all.text = "Deselect All SKUs"
      else:
        self.cb_select_all.text = "Select All SKUs"
    except AttributeError:
      pass

  @handle("cb_select_all", "change")
  def cb_select_all_change(self, **event_args):
    # v5.15: master select-all toggle.
    #
    # Sets every assignment's _selected to the master's new state, then
    # re-renders the items grid (so each RowTemplate2 picks up the new
    # flag and updates its checkbox) and refreshes the capacity summary.
    # No server call - purely client-side mutation of the in-memory
    # assignments list.
    try:
      new_state = bool(self.cb_select_all.checked)
    except AttributeError:
      # Shouldn't be reachable since the handler fired on cb_select_all
      # by definition, but defensive.
      return

    # v5.15: keep the master's label in sync with its new state, even
    # if the assignments list is empty (clicking the master on an empty
    # grid still toggles its visual state, so the label should follow).
    self._update_master_label()

    if not self.assignments:
      return

    for a in self.assignments:
      a['_selected'] = new_state

    # Re-render via shallow copy so Anvil's RepeatingPanel sees a fresh
    # items reference and re-instantiates row templates.
    self.rp_items.items = list(self.assignments)
    self.refresh_trip_summary()

  @handle("btn_trip_pdf", "click")
  def btn_trip_pdf_click(self, **event_args):
    """
    Handler for btn_trip_pdf click event.
    Generates and downloads the trip assignment PDF for selected batch.
    """
    selected_batch = self.dd_batch_pdf.selected_value

    if not selected_batch:
      alert("Please select a batch first")
      return

    # Show loading indicator
    self.btn_trip_pdf.enabled = False
    original_text = self.btn_trip_pdf.text
    self.btn_trip_pdf.text = "Generating PDF..."

    try:
      # Call server function with selected batch
      result = anvil.server.call('download_trip_pdf', batch_id=selected_batch)

      if result['status'] == 'success':
        # Trigger download
        anvil.media.download(result['pdf_media'])

        # Optional: Show success notification
        Notification("PDF generated successfully!", style="success").show()
      else:
        # Show error
        alert(f"Error generating PDF: {result['message']}")

    except Exception as e:
      alert(f"Error: {str(e)}")

    finally:
      # Reset button
      self.btn_trip_pdf.enabled = True
      self.btn_trip_pdf.text = original_text

  @handle("dp_date_pdf", "change")
  def dp_date_pdf_change(self, **event_args):
    """Called when date picker changes. Refresh batch list."""
    self.refresh_batch_dropdown()

  @handle("lk_transfers", "click")
  def lk_transfers_click(self, **event_args):
    """This method is called when the link is clicked"""
    pass

  @handle("dd_batch_pdf", "change")
  def dd_batch_pdf_change(self, **event_args):
    """This method is called when an item is selected"""
    pass