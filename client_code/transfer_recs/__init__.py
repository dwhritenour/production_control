from ._anvil_designer import transfer_recsTemplate
from anvil import *
import anvil.server


class transfer_recs(transfer_recsTemplate):
  def __init__(self, **properties):
    # Set Form properties and Data Bindings.
    self.init_components(**properties)
    self.nav.active = 'transfer_recs'

    # Initialize task tracking
    self.task_id = None

    # Wire up event handlers
    self.btn_refresh.set_event_handler('click', self.btn_refresh_click)
    self.btn_run_review.set_event_handler('click', self.btn_run_review_click)
    self.btn_apply.set_event_handler('click', self.btn_apply_click)
    self.btn_reject_all.set_event_handler('click', self.btn_reject_all_click)
    self.timer_check_task.set_event_handler('tick', self.timer_check_task_tick)

    # Load initial data
    self.load_transfers()
    self.refresh_review_stats()

  # =========================================================================
  # DATA LOADING
  # =========================================================================

  def load_transfers(self):
    """Fetch pending transfers from the server and populate the grid."""
    result = anvil.server.call('get_pending_transfers')
    if result['status'] == 'success':
      self.rp_transfers.items = result['rows']
    else:
      alert("Error loading transfers: " + result['message'])


  @handle("btn_refresh", "click")
  def btn_refresh_click(self, **event_args):
    """Refresh button - reload pending transfers and stats."""
    self.load_transfers()
    self.refresh_review_stats()
    Notification("Refreshed").show()

    # =========================================================================
    # REVIEW STATUS DISPLAY
    # =========================================================================

  def refresh_review_stats(self):
    """Fetch and display the last review run statistics."""
    try:
      stats = anvil.server.call('get_last_review_stats')

      if stats['status'] == 'success':
        # Format the timestamp nicely
        last_run = stats.get('last_run', '')
        if last_run:
          try:
            from datetime import datetime
            dt = datetime.fromisoformat(last_run.replace('Z', '+00:00'))
            last_run_display = dt.strftime('%b %d, %I:%M %p')
          except:
            last_run_display = last_run[:16]
        else:
          last_run_display = 'Never'

        # Include last run summary if available
        last_summary = stats.get('last_run_summary', '')
        if last_summary:
          self.lbl_last_run.text = "Last run: " + last_run_display + " (" + last_summary + ")"
        else:
          self.lbl_last_run.text = "Last run: " + last_run_display

        # Training mode: All items go to Pending for review
        self.lbl_stats.text = (
          f"Pending: {stats.get('pending', 0)} | "
          f"Paused: {stats.get('paused', 0)} | "
          f"Approved: {stats.get('approved', 0)} | "
          f"Applied: {stats.get('applied', 0)}"
        )
      elif stats['status'] == 'no_data':
        self.lbl_last_run.text = "Last run: Never"
        self.lbl_stats.text = "No reviews yet"
      else:
        self.lbl_last_run.text = "Last run: --"
        self.lbl_stats.text = "Error loading stats"
    except Exception as e:
      self.lbl_last_run.text = "Last run: --"
      self.lbl_stats.text = f"Error: {e}"

    # =========================================================================
    # MANUAL REVIEW TRIGGER
    # =========================================================================


  @handle("btn_run_review", "click")
  def btn_run_review_click(self, **event_args):
    """Manually trigger the AI review process."""
    if not confirm("Run transfer review now?\n\nThis will check for new transfer candidates and process them with AI."):
      return

      # Disable button while running
    self.btn_run_review.enabled = False
    self.btn_run_review.text = "Starting..."

    try:
      result = anvil.server.call('trigger_transfer_review')

      if result['status'] == 'launched':
        self.task_id = result['task_id']
        self.lbl_task_status.text = "Running..."
        self.lbl_task_status.visible = True
        self.btn_run_review.text = "Running..."

        # Start polling for status
        self.timer_check_task.interval = 2
      else:
        alert("Error starting review: " + result.get('message', 'Unknown error'))
        self.reset_review_button()

    except Exception as e:
      alert(f"Error: {e}")
      self.reset_review_button()


  @handle("timer_check_task", "tick")
  def timer_check_task_tick(self, **event_args):
    """Called every 2 seconds while a review task is running."""
    if not self.task_id:
      self.timer_check_task.interval = 0
      return

    try:
      status = anvil.server.call('get_review_task_status', self.task_id)

      if status.get('error'):
        self.timer_check_task.interval = 0
        self.task_complete("Error: " + status['error'])
        return

      state = status.get('state', {})

      if status.get('is_running', False):
        # Still running - update status display
        task_status = state.get('status', 'Running')
        progress = state.get('progress', '')
        current_sku = state.get('current_sku', '')

        if current_sku:
          self.lbl_task_status.text = task_status + ": " + current_sku + " (" + progress + ")"
        else:
          self.lbl_task_status.text = task_status + "..."
      else:
        # Task completed
        self.timer_check_task.interval = 0

        results = state.get('results', {})
        ai = results.get('ai_reviewed', 0)
        skipped = results.get('skipped', 0)
        errors = results.get('errors', 0)

        summary = f"AI Reviewed: {ai}"
        if skipped > 0:
          summary = f"{summary}, Skipped: {skipped}"
        if errors > 0:
          summary = f"{summary}, Errors: {errors}"

        self.task_complete(summary)

    except Exception as e:
      self.timer_check_task.interval = 0
      self.task_complete(f"Error: {e}")

  def task_complete(self, message):
    """Called when the background task finishes."""
    self.task_id = None
    self.reset_review_button()

    # Show completion message
    self.lbl_task_status.text = "Done: " + message

    # Refresh data
    self.refresh_review_stats()
    self.load_transfers()

    # Show notification
    Notification("Review complete: " + message).show()

  def reset_review_button(self):
    """Reset the review button to its default state."""
    self.btn_run_review.enabled = True
    self.btn_run_review.text = "Run Review Now"

    # =========================================================================
    # APPLY DECISIONS
    # =========================================================================

  @handle("btn_apply", "click")
  def btn_apply_click(self, **event_args):
    """
        Apply Decisions button:
        1. Collect all rows where a decision was made (Approve/Reject/Paused)
        2. Send to server for bulk update
        3. If any Approved, generate a Pick List PDF for download
           (temporary replacement for the CSV export while the e21
            tie-in is still pending)
        4. Refresh the grid
        """
    # Collect decisions from all rows
    updates = []

    for item in self.rp_transfers.items:
      decision = item.get('decision')
      if decision in ('Approved', 'Rejected', 'Paused'):
        update = {
          'recommendation_id': item['recommendation_id'],
          'decision': decision
        }
        # Include quantity override if provided
        qty_override = item.get('transfer_qty_override')
        if qty_override is not None:
          update['transfer_qty'] = qty_override
        updates.append(update)

    if not updates:
      alert("No decisions to apply. Please select Approve, Reject, or Paused for at least one item.")
      return

      # Confirm with user
    approved_count = sum(1 for u in updates if u['decision'] == 'Approved')
    rejected_count = sum(1 for u in updates if u['decision'] == 'Rejected')
    paused_count = sum(1 for u in updates if u['decision'] == 'Paused')

    confirm_msg = f"Apply {approved_count} approvals, {rejected_count} rejections"
    if paused_count > 0:
      confirm_msg += f", and {paused_count} paused"
    confirm_msg += "?"

    if not confirm(confirm_msg):
      return

      # Send bulk update to server
    result = anvil.server.call('bulk_update_decisions', updates)

    if result['status'] == 'error':
      alert(f"Error: {result['message']}")
      return

      # Show result
    Notification(f"Updated {result['updated_count']} recommendations").show()

    # If there were approvals, generate the Pick List PDF
    if approved_count > 0:
      if confirm("Generate Pick List (PDF) for approved transfers?"):
        self.generate_pick_list()

        # Refresh the grid to show remaining pending items
    self.load_transfers()
    self.refresh_review_stats()

  def generate_pick_list(self):
    """
        Apply all approved transfers and download the resulting Pick List PDF.
        This marks them as 'Applied' in the database.

        NOTE: This replaces the previous CSV-export workflow. The server
        function 'apply_transfers' still has the same name, but now returns
        a PDF under 'pdf_media' (aliased as 'csv_media' for compatibility).
        """
    result = anvil.server.call('apply_transfers')

    if result['status'] == 'error':
      alert("Error generating Pick List: " + result['message'])
      return

    # Prefer the new key, fall back to the legacy alias
    media = result.get('pdf_media') or result.get('csv_media')

    if media is None:
      Notification("No approved transfers to apply.").show()
      return

      # Trigger download of the Pick List PDF
    download(media)

    # Show summary
    Notification(
      f"Applied {result['applied_count']} transfers "
      f"({result['total_qty']} units) — Pick List generated"
    ).show()

    # Refresh grid
    self.load_transfers()
    self.refresh_review_stats()

  # Backward-compatibility shim: any lingering reference to generate_csv
  # will still produce the (new) Pick List PDF.
  def generate_csv(self):
    self.generate_pick_list()

    # =========================================================================
    # REJECT ALL
    # =========================================================================

  def btn_reject_all_click(self, **event_args):
    """Reject All button - mark all remaining pending items as Rejected."""
    # Count pending items
    pending_count = len(self.rp_transfers.items)

    if pending_count == 0:
      alert("No pending transfers to reject.")
      return

      # Confirm with user
    if not confirm(f"Reject all {pending_count} pending transfers?"):
      return

      # Call server
    result = anvil.server.call('reject_pending')

    if result['status'] == 'success':
      Notification(f"Rejected {result['rejected_count']} transfers").show()
      self.load_transfers()
      self.refresh_review_stats()
    else:
      alert(f"Error: {result['message']}")

  @handle("btn_transfer_form", "click")
  def btn_transfer_form_click(self, **event_args):
    """This method is called when the button is clicked"""
    # TODO: Implement transfer form functionality or remove button from UI
    Notification("Transfer form not yet implemented").show()

  @handle("timer_check_task", "show")
  def timer_check_task_show(self, **event_args):
    """This method is called when this timer's form is shown on the screen (or it is added to a visible form)"""
    pass

  @handle("timer_check_task", "hide")
  def timer_check_task_hide(self, **event_args):
    """This method is called when this timer's form is hidden from the screen (or it is removed from a visible form)"""
    pass