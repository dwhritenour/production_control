from ._anvil_designer import WorkOrderDetailTemplate
from anvil import *
import anvil.server
import anvil.media

from ...routing_status import routing_status


# Statuses from which the office may still cancel a work order.
CANCELLABLE_STATUSES = ('Created', 'Released', 'Assigned', 'In Progress', 'Paused')

# Statuses at (or beyond) Release — the routing panel is shown for these.
ROUTED_STATUSES = ('Released', 'Assigned', 'In Progress', 'Paused', 'Complete')


class WorkOrderDetail(WorkOrderDetailTemplate):
  """
    Work order detail popup.

    SE Department rollout (2026-06-17): the office no longer Starts/Completes
    work orders. The four action buttons now drive Release / Cancel:
       btn_release_wo          -> Release this WO          (Created -> Released)
       btn_cancel_wo         -> Cancel this WO           (-> Cancelled)
       btn_release_so_packet   -> Release ALL WOs for the SO
       btn_cancel_so_packet-> Cancel ALL WOs for the SO
    Print buttons (btn_print_wo / btn_print_so_packet) are unchanged.

    Routing v2 (2026-07-21): Release navigates to the full-page
    sku_details form (SKU Details / Routing Library) which owns the
    routing editor, Save & Release, and packet chaining. This popup no
    longer opens the routing dialog or flips the status itself — the
    v1 routing_editor dialog is retired. A WO still cannot reach
    'Released' without a saved routing (sku_details guarantees it).
    Released-and-later WOs show the routing_status panel at the bottom.
  """

  def __init__(self, item, **properties):
    self.init_components(**properties)
    self._item = item
    self._routing_panel = None
    self._populate()

  def _populate(self):
    """Fill in all detail fields from the work order item."""
    item = self._item

    rel = item.get('sales_order_rel') or '001'
    so_num = item.get('sales_order_number', '')
    self.lbl_detail_header.text = (
      f"Work Order Details — {item.get('work_order_number', '')}  "
      f"|  SO {so_num}-{rel}"
    )

    sku_level_notes = item.get('so_notes', '') or 'No SKU notes'
    self.lbl_detail_notes.content = (
      f"**SKU Notes:**&nbsp;&nbsp;&nbsp; {sku_level_notes}"
    )

    so_level_notes = item.get('so_level_notes', '') or 'No sales order notes'
    self.lbl_so_notes.content = (
      f"**Sales Order Notes:**&nbsp;&nbsp;&nbsp; {so_level_notes}"
    )

    standard = (item.get('standard_attrib_val') or '').strip()
    self.lbl_standard_config.content = (
      f"**Standard Config:**&nbsp;&nbsp;&nbsp; {standard or 'Not defined'}"
    )

    ordered = (item.get('so_attrib_val') or '').strip()
    self.lbl_ordered_config.content = (
      f"**Ordered Config:**&nbsp;&nbsp;&nbsp; {ordered or 'Not defined'}"
    )

    if not standard or not ordered:
      self.lbl_config_match.text = '⚠ Unable to compare — missing data'
      self.lbl_config_match.foreground = '#FF9800'
    elif standard == ordered:
      self.lbl_config_match.text = '✓ Standard Configuration'
      self.lbl_config_match.foreground = '#4CAF50'
    else:
      self.lbl_config_match.text = '⚠ STRAINING ELEMENT SPECIAL CONFIGURATION'
      self.lbl_config_match.foreground = '#F44336'

    # ---------------------------------------------------------------
    # Action captions + visibility (Release / Cancel)
    # ---------------------------------------------------------------
    status = item.get('status', '')
    # Single WO -- btn_release_wo is dual-purpose:
    #   Created  -> "Release WO"   (Created  -> Released)
    #   Released -> "UnRelease WO" (Released -> Created)  [added 2026-06-23]
    if status == 'Created':
      self.btn_release_wo.text = 'Release WO'
      self.btn_release_wo.visible = True
    elif status == 'Released':
      self.btn_release_wo.text = 'UnRelease WO'
      self.btn_release_wo.visible = True
    else:
      self.btn_release_wo.visible = False
    self.btn_cancel_wo.text = 'Cancel WO'
    self.btn_cancel_wo.visible = (status in CANCELLABLE_STATUSES)
    # SO packet buttons (always available; server reports how many were
    # affected). btn_release_so_packet is dual-purpose, keyed off THIS WO's
    # status -- an SO can hold mixed states, so we follow the opened WO, the
    # same rule as the single Release/UnRelease button:
    #   opened WO Released -> "UnRelease SO Packet" (Released -> Created)
    #   otherwise          -> "Release SO Packet"   (Created  -> Released)
    if status == 'Released':
      self.btn_release_so_packet.text = 'UnRelease SO Packet'
    else:
      self.btn_release_so_packet.text = 'Release SO Packet'
    self.btn_cancel_so_packet.text = 'Cancel SO Packet'

    # ---------------------------------------------------------------
    # Routing panel (Routing v1, unchanged): Released-and-later WOs.
    # Rework reset is offered while the WO sits at 'Released'.
    # ---------------------------------------------------------------
    if status in ROUTED_STATUSES and self._routing_panel is None:
      self._routing_panel = routing_status(
        self._item['work_order_id'],
        show_rework=(status == 'Released'))
      self.add_component(self._routing_panel)

  # =========================================================================
  # SINGLE WO: RELEASE / UNRELEASE  (btn_release_wo, dual-purpose)
  #   Created  -> navigate to sku_details (WO mode) — it saves the routing
  #               and flips the status (Routing v2)
  #   Released -> Created (UnRelease; routing snapshot is kept)
  # =========================================================================
  @handle("btn_release_wo", "click")
  def btn_release_wo_click(self, **event_args):
    status = self._item.get('status', '')
    wo_num = self._item.get('work_order_number', '')

    if status == 'Released':
      if not confirm(
        f"Un-release {wo_num} back to 'Created'?\n\n"
        "It will be removed from the SE Department board until it is "
        "released again. Its routing is kept and will pre-load at the "
        "next release."
      ):
        return
      result = anvil.server.call('unrelease_work_order',
                                 self._item['work_order_id'])
      if result['status'] == 'success':
        Notification(f"{wo_num} un-released (back to Created)").show()
        self.raise_event('x-close')
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")
      return

    # Routing v2: the SKU Details page owns routing + release.
    # No 'x-close' here: we are NAVIGATING AWAY, and the host row's
    # refresh handler would run get_open_form().refresh_data() against
    # sku_details (which has no refresh_data). Just dismiss + navigate.
    self.raise_event('x-close-alert')    # DISMISS this popup (Anvil built-in)
    open_form('sku_details',
              work_order_id=self._item['work_order_id'],
              wo_queue=[])

  # =========================================================================
  # SINGLE WO: CANCEL  (btn_cancel_wo)  any live status -> Cancelled
  # =========================================================================
  @handle("btn_cancel_wo", "click")
  def btn_cancel_wo_click(self, **event_args):
    status = self._item.get('status', '')
    wo_num = self._item.get('work_order_number', '')
    if status in ('In Progress', 'Paused'):
      msg = (f"{wo_num} is currently {status} on the floor.\n\n"
             "Cancelling will remove it from the SE Department board "
             "immediately. Continue?")
    else:
      msg = f"Cancel {wo_num}? This cannot be undone."
    if not confirm(msg):
      return
    result = anvil.server.call(
      'update_work_order_status',
      self._item['work_order_id'],
      'Cancelled'
    )
    if result['status'] == 'success':
      Notification(f"{wo_num} cancelled").show()
      self.raise_event('x-close')
    else:
      alert(f"Error: {result.get('message', 'Unknown error')}")

  # =========================================================================
  # SO PACKET: RELEASE / UN-RELEASE ALL  (btn_release_so_packet, dual-purpose)
  #   opened WO Released -> UnRelease packet (Released -> Created, unchanged)
  #   otherwise          -> navigate to sku_details with the packet queue
  # =========================================================================
  @handle("btn_release_so_packet", "click")
  def btn_release_so_packet_click(self, **event_args):
    so_num = self._item.get('sales_order_number', '')
    rel = self._item.get('sales_order_rel') or '001'
    unrelease_mode = (self._item.get('status', '') == 'Released')

    if unrelease_mode:
      if not confirm(
        f"Un-release all work orders for SO {so_num}-{rel}?\n\n"
        f"This pulls every 'Released' work order back to 'Created' and off "
        f"the SE Department board. Work orders already on the floor "
        f"(Assigned / In Progress / Paused) are left in place. Routings "
        f"are kept."
      ):
        return
      self.btn_release_so_packet.enabled = False
      self.btn_release_so_packet.text = "Un-releasing..."
      try:
        result = anvil.server.call('unrelease_so_work_orders', so_num, rel)
        if result['status'] == 'success':
          msg = result.get('message', '')
          skipped = result.get('skipped_on_floor', 0)
          if skipped > 0:
            msg += f" ({skipped} left on the floor)"
          Notification(msg).show()
          self.raise_event('x-close')
        else:
          alert(f"Error: {result.get('message', 'Unknown error')}")
      finally:
        self.btn_release_so_packet.enabled = True
        self.btn_release_so_packet.text = "UnRelease SO Packet"
      return

    # ---- Release mode (Routing v2): hand the packet to sku_details ----
    listing = anvil.server.call('get_so_created_work_orders', so_num, rel)
    if listing.get('status') != 'success':
      alert(f"Error: {listing.get('message', 'Unknown error')}")
      return
    wos = listing.get('work_orders', [])
    if not wos:
      Notification(f"No 'Created' work orders on SO {so_num}-{rel}.").show()
      return

    if not confirm(
      f"Release all {len(wos)} 'Created' work order(s) for SO {so_num}-{rel}?\n\n"
      f"Each work order needs a routing — the Routing page will walk you "
      f"through them. Work orders sharing a part can reuse the first routing."
    ):
      return

    # No 'x-close' here: we are NAVIGATING AWAY, and the host row's
    # refresh handler would run get_open_form().refresh_data() against
    # sku_details (which has no refresh_data). Just dismiss + navigate.
    self.raise_event('x-close-alert')    # DISMISS this popup (Anvil built-in)
    open_form('sku_details',
              work_order_id=wos[0]['work_order_id'],
              wo_queue=wos[1:])

  # =========================================================================
  # SO PACKET: CANCEL ALL  (btn_cancel_so_packet)
  # =========================================================================
  @handle("btn_cancel_so_packet", "click")
  def btn_cancel_so_packet_click(self, **event_args):
    so_num = self._item.get('sales_order_number', '')
    rel = self._item.get('sales_order_rel') or '001'

    # Preview exactly which live WOs this will cancel, so the office sees the
    # full blast radius before confirming -- Cancel SO Packet hits the WHOLE
    # order, not just the line this popup was opened from.
    preview = anvil.server.call('get_so_cancellable_work_orders', so_num, rel)
    if preview.get('status') != 'success':
      alert(f"Error: {preview.get('message', 'Unknown error')}")
      return
    live = preview.get('work_orders', [])
    if not live:
      Notification(f"No live work orders to cancel on SO {so_num}-{rel}.").show()
      return

    _MAX_SHOWN = 20
    listing_rows = [
      f"  • {w.get('work_order_number', '')}  (Line "
      f"{w.get('sales_order_line', '')}) — {w.get('status', '')}"
      for w in live[:_MAX_SHOWN]]
    if len(live) > _MAX_SHOWN:
      listing_rows.append(f"  … and {len(live) - _MAX_SHOWN} more")
    listing = "\n".join(listing_rows)

    if not confirm(
      f"Cancel ALL {len(live)} live work order(s) for SO {so_num}-{rel}?\n\n"
      f"{listing}\n\n"
      f"This cancels the entire order's work orders (not just this line) and "
      f"cannot be undone."
    ):
      return

    self.btn_cancel_so_packet.enabled = False
    self.btn_cancel_so_packet.text = "Cancelling..."
    try:
      result = anvil.server.call('cancel_so_work_orders', so_num, rel)
      if result['status'] == 'success':
        skipped = result.get('skipped', 0)
        msg = result.get('message', '')
        if skipped > 0:
          msg += f" ({skipped} already complete/cancelled — skipped)"
        Notification(msg).show()
        self.raise_event('x-close')
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")
    finally:
      self.btn_cancel_so_packet.enabled = True
      self.btn_cancel_so_packet.text = "Cancel SO Packet"

  # =========================================================================
  # PRINT  (unchanged)
  # =========================================================================
  @handle("btn_print_wo", "click")
  def btn_print_wo_click(self, **event_args):
    self.btn_print_wo.enabled = False
    self.btn_print_wo.text = "Generating..."
    try:
      result = anvil.server.call(
        'download_work_order_pdf',
        self._item['work_order_id']
      )
      if result['status'] == 'success':
        anvil.media.download(result['pdf_media'])
        Notification("Work order PDF downloaded").show()
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")
    finally:
      self.btn_print_wo.enabled = True
      self.btn_print_wo.text = "Print This WO"

  @handle("btn_print_so_packet", "click")
  def btn_print_so_packet_click(self, **event_args):
    so_num = self._item.get('sales_order_number', '')
    rel = self._item.get('sales_order_rel') or '001'
    self.btn_print_so_packet.enabled = False
    self.btn_print_so_packet.text = "Generating Packet..."
    try:
      result = anvil.server.call('download_so_packet_pdf', so_num, rel)
      if result['status'] == 'success':
        anvil.media.download(result['pdf_media'])
        Notification(f"SO {so_num}-{rel} packet downloaded").show()
      else:
        alert(f"Error: {result.get('message', 'Unknown error')}")
    finally:
      self.btn_print_so_packet.enabled = True
      self.btn_print_so_packet.text = "Print SO Packet"
