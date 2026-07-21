from ._anvil_designer import sku_detailsTemplate
from anvil import *
import anvil.server


class sku_details(sku_detailsTemplate):
  """
    SKU Details / Routing Library (Routing v2) — the app's centerpiece.

    LIBRARY mode:  open_form('sku_details')
                   search any SKU, curate its default routing.
    WO mode:       open_form('sku_details', work_order_id=..., wo_queue=[...])
                   Release navigates here; Save & Release gates the status
                   flip. wo_queue = [{'work_order_id','work_order_number',
                   'part_number'}, ...] chains packet releases.

    Working-list pattern: self._steps is a list of
      {'seq': int, 'department_id': int|None, 'step_notes': str}
    Steps sharing a seq run in PARALLEL. Nothing persists until Save.
  """

  def __init__(self, work_order_id=None, wo_queue=None, sku=None, **properties):
    self.init_components(**properties)
    self._wo = None
    self._wo_queue = list(wo_queue or [])
    self._part = None
    self._steps = []
    self._derived = []
    self._template_target = None      # PC SKU minted from this WO (see _new_sku)
    self._part_routings = {}          # packet shortcut: part_number -> payload

    self._departments = anvil.server.call('get_departments')
    self._dd_items = [(d['department_name'], d['department_id'])
                      for d in self._departments]
    self._dept_names = {d['department_id']: d['department_name']
                        for d in self._departments}

    # Events (all wiring in code — shells are layout-only)
    self.tb_sku_search.set_event_handler('pressed_enter', self._search)
    self.dd_sku_results.set_event_handler('change', self._pick_result)
    self.btn_new_sku.set_event_handler('click', self._new_sku_click)
    self.lk_view_notes.set_event_handler('click', self._view_notes_click)
    self.btn_add_step.set_event_handler('click', self._add_step_click)
    self.btn_save_template.set_event_handler('click', self._save_template_click)
    self.btn_save_release.set_event_handler('click', self._save_release_click)
    self.btn_cancel_wo.set_event_handler('click', self._cancel_wo_click)
    # Rows call self._step_changed / self._remove_step via callbacks in the
    # item dict (event bubbling through this runtime's wrapper nesting is
    # unreliable - see RoutingStepRow2 docstring).

    self.btn_save_release.background = '#1565C0'   # medium blue
    self.btn_save_release.foreground = '#FFFFFF'   # white

    if work_order_id:
      self._enter_wo_mode(work_order_id)
    elif sku:
      self._load_sku(sku)
      self._set_library_chrome()
    else:
      self._set_library_chrome()
      self.lbl_sku_title.text = 'Routing Library'
      self.lbl_part_desc.text = 'Search a SKU to view or build its default routing.'
      self.lbl_sku_source.visible = False
      self._render()

  # ===================================================================
  # Mode chrome
  # ===================================================================
  def _set_library_chrome(self):
    self.fp_search.visible = True
    self.tb_sku_search.visible = True
    self.dd_sku_results.visible = True
    self.lbl_wo_banner.visible = False
    self.lk_view_notes.visible = False
    self.btn_save_release.visible = False
    self.btn_cancel_wo.visible = False
    self.btn_save_template.visible = True

  def _enter_wo_mode(self, work_order_id):
    payload = anvil.server.call('get_routing_for_release', work_order_id)
    if payload.get('status') != 'success':
      alert(f"Error: {payload.get('message', 'Unknown error')}")
      open_form('work_orders')
      return
    self._wo = payload['wo']
    self._derived = payload.get('derived_specials', [])
    self._template_target = None
    wo = self._wo

    due = wo.get('wo_due_date') or wo.get('due_date') or 'n/a'
    self.lbl_wo_banner.text = (
      f"Routing for WO {wo.get('work_order_number', '')}  |  "
      f"SO {wo.get('sales_order_number', '')}-{wo.get('sales_order_rel', '')}  |  "
      f"Qty {wo.get('effective_qty', '')}  |  Due {due}")
    self.lbl_wo_banner.background = '#E3F2FD'   # pale sky blue
    self.lbl_wo_banner.visible = True
    # WO mode is locked to this WO's part: hide the SEARCH controls only.
    # fp_search stays visible because it also holds btn_new_sku - minting
    # a special SKU from a live work order is the primary use case.
    self.tb_sku_search.visible = False
    self.dd_sku_results.visible = False
    self.lk_view_notes.visible = True
    self.btn_save_release.visible = True
    self.btn_cancel_wo.visible = True
    self.btn_save_template.visible = False      # WO mode uses the save prompt

    self._show_part_header(wo['part_number'])
    self._steps = self._steps_from_rows(payload.get('steps', []))
    self._render()

    # §2.5 derived specials — only when no prior WO routing exists
    if self._derived and payload.get('source') != 'wo':
      self._offer_derived_specials()

  # ===================================================================
  # Part header / SKU loading
  # ===================================================================
  def _show_part_header(self, sku):
    detail = anvil.server.call('get_part_detail', sku)
    if detail.get('status') != 'success':
      self._part = {'sku': sku, 'part_desc': '(not in part master)',
                    'sku_source': 'e21'}
      self.lbl_sku_title.text = f'SKU: {sku}'
      self.lbl_part_desc.text = '(not in part master)'
      self.lbl_sku_source.visible = False
      return
    self._part = detail['part']
    p = self._part
    self.lbl_sku_title.text = f"SKU: {p['sku']}"
    bits = [p.get('part_desc') or '', p.get('body_mat') or '',
            p.get('size') or '', p.get('series') or '']
    self.lbl_part_desc.text = '  |  '.join(b for b in bits if b)
    src = p.get('sku_source', 'e21')
    self.lbl_sku_source.text = 'PC-created' if src == 'PC' else 'e21'
    if src == 'PC':
      self.lbl_sku_source.background = '#E3F2FD'   # pale sky blue
      self.lbl_sku_source.foreground = '#1565C0'   # medium blue
    else:
      self.lbl_sku_source.background = '#ECEFF1'   # very pale gray-blue
      self.lbl_sku_source.foreground = '#444444'   # dark gray
    self.lbl_sku_source.visible = True

  def _load_sku(self, sku):
    """LIBRARY mode: load a SKU and its default template."""
    self._show_part_header(sku)
    tpl = anvil.server.call('get_template_for_sku', sku)
    self._steps = self._steps_from_rows(tpl.get('steps', []))
    self._render()

  def _search(self, **event_args):
    hits = anvil.server.call('search_part_master', self.tb_sku_search.text)
    if not hits:
      Notification('No SKUs match that search.').show()
      self.dd_sku_results.items = []
      return
    self.dd_sku_results.items = [
      (f"{h['sku']} — {h['part_desc']}"
       + ('  [PC]' if h.get('sku_source') == 'PC' else ''), h['sku'])
      for h in hits]
    self.dd_sku_results.selected_value = None

  def _pick_result(self, **event_args):
    if self.dd_sku_results.selected_value:
      self._load_sku(self.dd_sku_results.selected_value)

  # ===================================================================
  # Working list <-> display
  # ===================================================================
  def _steps_from_rows(self, rows):
    """DB rows (gapped sequence_numbers) -> working list with display
    numbers 1..n; rows sharing a sequence_number share a display number."""
    seqs = sorted({r['sequence_number'] for r in rows})
    disp = {s: i + 1 for i, s in enumerate(seqs)}
    return [{'seq': disp[r['sequence_number']],
             'department_id': r['department_id'],
             'step_notes': (r.get('step_notes') or '')}
            for r in rows]

  def _render(self):
    self._steps.sort(key=lambda s: s['seq'])   # stable: keeps entry order in group
    self.rp_steps.items = [
      {'index': i, 'seq': s['seq'], 'department_id': s['department_id'],
       'step_notes': s['step_notes'], 'dd_items': self._dd_items,
       'step_ref': s,
       'on_change': self._step_changed, 'on_remove': self._remove_step}
      for i, s in enumerate(self._steps)]
    has_steps = bool(self._steps)
    self.btn_save_template.enabled = has_steps and self._part is not None
    self.btn_save_release.enabled = has_steps
    self.html_diagram.html = self._build_svg()

  def _step_changed(self, **event_args):
    self._render()

  def _remove_step(self, index, **event_args):
    del self._steps[index]
    self._render()

  def _add_step_click(self, **event_args):
    next_seq = (max(s['seq'] for s in self._steps) + 1) if self._steps else 1
    self._steps.append({'seq': next_seq, 'department_id': None, 'step_notes': ''})
    self._render()

  # ===================================================================
  # Flow diagram (client-built SVG — redraws on every change)
  # ===================================================================
  def _build_svg(self):
    if not self._steps:
      return ('<div style="color:#999999;font-style:italic;padding:24px;'
              'font-family:Arial;">Add steps to see the flow</div>')
    bands = {}
    for s in self._steps:
      bands.setdefault(s['seq'], []).append(s)
    order = sorted(bands.keys())
    NW, NH, VGAP, HGAP, PAD = 150, 44, 48, 14, 10
    max_n = max(len(v) for v in bands.values())
    width = max(340, max_n * (NW + HGAP) - HGAP + 2 * PAD)
    height = len(order) * NH + (len(order) - 1) * VGAP + 2 * PAD
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'style="width:100%;max-width:{width}px;font-family:Arial;">',
           '<defs><marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" '
           'markerWidth="6" markerHeight="6" orient="auto">'
           '<path d="M0 0L10 5L0 10z" fill="#666666"/></marker></defs>']
    centers = []   # per band: (list of x-centers, top_y, bottom_y)
    for bi, key in enumerate(order):
      row = bands[key]
      row_w = len(row) * (NW + HGAP) - HGAP
      x0 = (width - row_w) / 2
      y = PAD + bi * (NH + VGAP)
      xs = []
      for ni, s in enumerate(row):
        x = x0 + ni * (NW + HGAP)
        name = self._dept_names.get(s['department_id'], '(pick dept)')
        label = f"{bi + 1}. {name}"
        if len(label) > 19:
          label = label[:18] + '…'
        out.append(f'<rect x="{x:.0f}" y="{y}" width="{NW}" height="{NH}" rx="10" '
                   f'fill="#E3F2FD" stroke="#1565C0" stroke-width="2"/>')
        out.append(f'<text x="{x + NW/2:.0f}" y="{y + NH/2 + 5}" font-size="13" '
                   f'fill="#212121" text-anchor="middle">{label}</text>')
        xs.append(x + NW / 2)
      centers.append((xs, y, y + NH))
    for bi in range(len(centers) - 1):
      xs_a, _, bot = centers[bi]
      xs_b, top, _ = centers[bi + 1]
      for xa in xs_a:
        for xb in xs_b:
          out.append(f'<line x1="{xa:.0f}" y1="{bot}" x2="{xb:.0f}" y2="{top - 2}" '
                     f'stroke="#666666" stroke-width="2" marker-end="url(#arw)"/>')
    out.append('</svg>')
    # Wrap in a div: Anvil's HTML component sets className on the root
    # element, and SVG roots expose className as READ-ONLY (browser
    # TypeError). A div root keeps Anvil happy; the SVG lives inside it.
    return '<div>' + ''.join(out) + '</div>'

  # ===================================================================
  # Derived specials (§2.5) — suggestion, never substitution
  # ===================================================================
  def _offer_derived_specials(self):
    # Deterministic attribute match first (v2.1): the server compared the
    # WO's ORDERED config to each special's stored attributes.
    matches = [d for d in self._derived if d.get('attrib_match')]
    if matches:
      m = matches[0]
      wo_attrib = self._wo.get('attribute_value') or ''
      if confirm(f"This order's configuration ({wo_attrib}) exactly "
                 f"matches PC special {m['sku']} — {m['part_desc']}.\n\n"
                 f"Load its routing?"):
        tpl = anvil.server.call('get_template_for_sku', m['sku'])
        if tpl.get('steps'):
          self._steps = self._steps_from_rows(tpl['steps'])
          self._template_target = m['sku']
          Notification(f"Loaded routing from {m['sku']}").show()
          self._render()
      return   # matched (loaded or declined) - no second prompt

    with_tpl = [d for d in self._derived if d.get('has_template')]
    if not with_tpl:
      return
    buttons = [(f"{d['sku']} — {d['part_desc']}", d['sku']) for d in with_tpl]
    buttons.append(("Not a special / skip", None))
    choice = alert(
      (f"{len(with_tpl)} PC-created special(s) derive from "
       f"{self._wo['part_number']}. Is this order one of them?\n\n"
       f"(Loads that special's routing — the work order's part number "
       f"is not changed.)"),
      title="Derived specials", buttons=buttons)
    if choice:
      tpl = anvil.server.call('get_template_for_sku', choice)
      if tpl.get('steps'):
        self._steps = self._steps_from_rows(tpl['steps'])
        self._template_target = choice   # save-prompt targets the special
        Notification(f"Loaded routing from {choice}").show()
        self._render()

  # ===================================================================
  # Validation + payload
  # ===================================================================
  def _validated_payload(self):
    if not self._steps:
      alert("Add at least one routing step.")
      return None
    if any(not s['department_id'] for s in self._steps):
      alert("Every step needs a department selected.")
      return None
    # duplicate department within the SAME parallel group is usually a mis-click
    seen = set()
    for s in self._steps:
      key = (s['seq'], s['department_id'])
      if key in seen:
        if not confirm("The same department appears twice in one parallel "
                       "group. That's allowed — save anyway?"):
          return None
        break
      seen.add(key)
    return [{'seq_group': s['seq'], 'department_id': s['department_id'],
             'step_notes': s['step_notes']} for s in self._steps]

  # ===================================================================
  # LIBRARY mode: save default template
  # ===================================================================
  def _save_template_click(self, **event_args):
    if not self._part:
      alert("Load a SKU first.")
      return
    payload = self._validated_payload()
    if payload is None:
      return
    result = anvil.server.call('save_routing_template',
                               self._part['sku'], payload)
    if result.get('status') == 'success':
      Notification(f"Default routing saved for {self._part['sku']}").show()
    else:
      alert(f"Error: {result.get('message', 'Unknown error')}")

  # ===================================================================
  # WO mode: save + release (+ packet chaining)
  # ===================================================================
  def _save_release_click(self, **event_args):
    payload = self._validated_payload()
    if payload is None:
      return
    wo_id = self._wo['work_order_id']
    wo_num = self._wo.get('work_order_number', '')

    saved = anvil.server.call('save_wo_routing', wo_id, payload)
    if saved.get('status') != 'success':
      alert(f"Error: {saved.get('message', 'Unknown error')}")
      return

    # Template prompt: targets the minted/loaded special when one is in
    # play (banks the knowledge under the SPECIAL), else the WO's part.
    tpl_sku = self._template_target or self._wo['part_number']
    if saved.get('template_differs') or self._template_target:
      if confirm(f"Save this routing as the default for {tpl_sku}?\n\n"
                 f"Answer No for a one-off deviation on this order only."):
        tpl = anvil.server.call('save_routing_template', tpl_sku, payload)
        if tpl.get('status') != 'success':
          alert(f"Routing saved, but the default was not: "
                f"{tpl.get('message', 'Unknown error')}")

    rel = anvil.server.call('update_work_order_status', wo_id, 'Released')
    if rel.get('status') != 'success':
      alert(f"Routing saved but release failed: "
            f"{rel.get('message', 'Unknown error')}")
      return
    Notification(f"WO {wo_num} released to the floor").show()

    # Packet shortcut: offer reuse for remaining same-part WOs
    part = self._wo['part_number']
    remaining_same = [w for w in self._wo_queue
                      if w.get('part_number') == part]
    if remaining_same and part not in self._part_routings:
      if confirm(f"Apply this same routing to the remaining "
                 f"{len(remaining_same)} work order(s) for part {part} "
                 f"in this packet?"):
        self._part_routings[part] = payload
    self._advance_queue()

  def _cancel_wo_click(self, **event_args):
    wo_num = self._wo.get('work_order_number', '') if self._wo else ''
    if self._wo_queue:
      c = confirm(f"WO {wo_num} was not released.\n\nSkip it and continue "
                  f"with the rest of the packet?  (Cancel stops the whole "
                  f"packet release.)")
      if c:
        self._advance_queue()
        return
    open_form('work_orders')

  def _advance_queue(self):
    """Release chained packet WOs: auto-apply stored same-part routings,
    open the editor for anything else, return to work_orders when done."""
    while self._wo_queue:
      nxt = self._wo_queue.pop(0)
      part = nxt.get('part_number')
      if part in self._part_routings:
        saved = anvil.server.call('save_wo_routing', nxt['work_order_id'],
                                  self._part_routings[part])
        if saved.get('status') == 'success':
          rel = anvil.server.call('update_work_order_status',
                                  nxt['work_order_id'], 'Released')
          if rel.get('status') == 'success':
            Notification(f"WO {nxt.get('work_order_number', '')} released "
                         f"(routing reused)").show()
            continue
        alert(f"WO {nxt.get('work_order_number', '')}: "
              f"{saved.get('message', 'could not reuse routing')} — "
              f"opening the editor.")
      self._enter_wo_mode(nxt['work_order_id'])
      return
    open_form('work_orders')

  # ===================================================================
  # SO notes popup
  # ===================================================================
  def _view_notes_click(self, **event_args):
    so = self._wo.get('sales_order_number', '')
    rel = self._wo.get('sales_order_rel', '')
    result = anvil.server.call('get_so_all_notes', so, rel)
    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return

    panel = ColumnPanel()
    if result.get('message'):
      panel.add_component(Label(text=result['message'], italic=True))
    if result.get('header_notes'):
      panel.add_component(Label(text='Sales Order Notes', bold=True))
      panel.add_component(Label(text=result['header_notes'],
                                foreground='#333333'))
    my_line = str(self._wo.get('sales_order_line', ''))
    for ln in result.get('lines', []):
      is_mine = str(ln['line_no']).lstrip('0') == my_line.lstrip('0')
      hdr = Label(text=(f"Line {ln['line_no']} — {ln['sku']}  "
                        f"(qty {ln['qty']})" + ('   ← this WO' if is_mine else '')),
                  bold=True)
      if is_mine:
        hdr.background = '#FFF8E1'   # pale warm amber
      panel.add_component(hdr)
      if ln.get('attrib_val'):
        panel.add_component(Label(text=f"Config: {ln['attrib_val']}",
                                  italic=True, foreground='#595959'))
      panel.add_component(Label(text=(ln.get('notes') or '(no notes)'),
                                foreground='#333333'))
    alert(content=panel, title=f"All notes — SO {so}-{rel}",
          large=True, buttons=[("Close", None)])

  # ===================================================================
  # New SKU dialog (code-built)
  # ===================================================================
  def _new_sku_click(self, **event_args):
    base = self._part or {}
    tb_sku = TextBox(placeholder='New SKU (e21 pattern)',
                     text=(base.get('sku') or ''))
    tb_desc = TextBox(placeholder='Description',
                      text=(base.get('part_desc') or ''))
    tb_series = TextBox(placeholder='Series', text=(base.get('series') or ''))
    tb_mat = TextBox(placeholder='Body material', text=(base.get('body_mat') or ''))
    tb_size = TextBox(placeholder='Size', text=(base.get('size') or ''))
    # Attributes drive the automatic match on future orders. Prefill from
    # the WO's ORDERED config (WO mode) or the loaded part's attributes.
    attrib_seed = ''
    if self._wo:
      attrib_seed = self._wo.get('attribute_value') or ''
    if not attrib_seed:
      attrib_seed = base.get('attrib_value') or ''
    tb_attrib = TextBox(placeholder='Attributes (MATERIAL | SE), e.g. '
                        'SS T304 | 1/32 PERF',
                        text=attrib_seed)
    ta_special = TextArea(placeholder='What makes it special (paste from '
                          'View Notes)', height=90)
    panel = ColumnPanel()
    panel.add_component(Label(
      text=(f"Base SKU: {base.get('sku', '(none)')} — edit the number to "
            f"the e21 pattern; it must be unique."), bold=True))
    for c in (tb_sku, tb_desc, tb_series, tb_mat, tb_size, tb_attrib, ta_special):
      panel.add_component(c)

    if not alert(content=panel, title="Create PC SKU",
                 buttons=[("Create", True), ("Cancel", False)], large=True):
      return
    result = anvil.server.call('create_pc_sku', {
      'sku': tb_sku.text, 'part_desc': tb_desc.text,
      'series': tb_series.text, 'body_mat': tb_mat.text,
      'size': tb_size.text,
      'line': base.get('line'), 'model': base.get('model'),
      'asme_class': base.get('asme_class'),
      'end_connect': base.get('end_connect'),
      'base_sku': base.get('sku'),
      'attrib_value': tb_attrib.text,
      'special_notes': ta_special.text,
    })
    if result.get('status') != 'success':
      alert(f"Error: {result.get('message', 'Unknown error')}")
      return
    new_sku = result['part']['sku']
    Notification(f"SKU {new_sku} created").show()
    if self._wo:
      # WO mode: keep working this WO; the save prompt now banks the
      # routing under the NEW special (§2.5 lineage).
      self._template_target = new_sku
      self._show_part_header(new_sku)
      self.lbl_wo_banner.visible = True   # still releasing the same WO
    else:
      self._load_sku(new_sku)
