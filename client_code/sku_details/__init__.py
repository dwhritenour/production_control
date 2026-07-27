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
    self._dept_codes = {d['department_id']: (d.get('dept_code') or '?')
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
      f"Routing for {wo.get('work_order_number', '')}  |  "
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

  def _step_codes(self):
    """A01.01 codes for the (sorted) working list. Letter = department,
    2-digit = routing step (sequence group), .2-digit = that DEPARTMENT'S
    step count across the WHOLE routing (Dave 2026-07-23: F04.01 then
    F05.02 - Material Handling's first and second steps)."""
    disp = {g: i + 1 for i, g in enumerate(sorted({s['seq'] for s in self._steps}))}
    counters, codes = {}, []
    for s in self._steps:
      gd = disp[s['seq']]
      dept = s['department_id']
      counters[dept] = counters.get(dept, 0) + 1
      letter = self._dept_codes.get(dept, '?')
      codes.append(f"{letter}{gd:02d}.{counters[dept]:02d}")
    return codes

  def _render(self):
    self._steps.sort(key=lambda s: s['seq'])   # stable: keeps entry order in group
    codes = self._step_codes()
    self.rp_steps.items = [
      {'index': i, 'seq': s['seq'], 'department_id': s['department_id'],
       'step_notes': s['step_notes'], 'dd_items': self._dd_items,
       'step_ref': s, 'code': codes[i],
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
  # Lane colors keyed by dept_code: (node fill, stroke/header text).
  # Identity is carried by the letter + header TEXT - color is decoration.
  _LANE_COLORS = {
    'A': ('#FCE4E4', '#C62828'), 'B': ('#FCE4F1', '#AD1457'),
    'C': ('#EDE7F6', '#6A1B9A'), 'D': ('#E8EAF6', '#283593'),
    'E': ('#E3F2FD', '#1565C0'), 'F': ('#E0F2F1', '#00695C'),
    'G': ('#E8F5E9', '#2E7D32'), 'H': ('#FFF3E0', '#EF6C00'),
  }

  def _build_svg(self):
    """Swim-lane routing diagram (Dave's sample, 2026-07-22): one lane
    per department (ALL lanes, always - spatial consistency), flow top
    to bottom, nodes carry A01.01 codes. Redraws on every change."""
    lanes = self._departments                     # sort_order = lane order
    LW, NW, NH, VGAP, PAD = 140, 112, 40, 34, 8
    # Header: big letter on top, then EACH WORD of the department name on
    # its own line (Dave's format, 2026-07-22). Height fits the wordiest name.
    max_words = max((len(d['department_name'].split()) for d in lanes),
                    default=1)
    HH = 24 + max_words * 13 + 5
    width = len(lanes) * LW
    lane_x = {d['department_id']: i for i, d in enumerate(lanes)}

    if not self._steps:
      return ('<div style="color:#999999;font-style:italic;padding:24px;'
              'font-family:Arial;">Add steps to see the flow</div>')

    codes = self._step_codes()
    disp = {g: i + 1 for i, g in enumerate(sorted({s['seq'] for s in self._steps}))}
    # group -> {lane_index: [(step, code), ...]}
    groups = {}
    for s, c in zip(self._steps, codes):
      gd = disp[s['seq']]
      li = lane_x.get(s['department_id'], 0)
      groups.setdefault(gd, {}).setdefault(li, []).append((s, c))

    body = []
    # Lane headers + separators (heights fixed after we know total height)
    y = HH + PAD
    node_rows = []                                # per group: list of node dicts
    for gd in sorted(groups.keys()):
      lanes_here = groups[gd]
      depth = max(len(v) for v in lanes_here.values())
      nodes = []
      for li, entries in lanes_here.items():
        for k, (s, code) in enumerate(entries):
          nx = li * LW + (LW - NW) / 2
          ny = y + k * (NH + 6)
          d = lanes[li]
          fill, stroke = self._LANE_COLORS.get(d.get('dept_code') or '?',
                                               ('#F5F5F5', '#616161'))
          note = (s.get('step_notes') or '').strip()
          if len(note) > 16:
            note = note[:15] + '\u2026'
          body.append(f'<rect x="{nx:.0f}" y="{ny:.0f}" width="{NW}" height="{NH}" '
                      f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
          body.append(f'<text x="{nx + NW/2:.0f}" y="{ny + 16:.0f}" font-size="12" '
                      f'font-weight="bold" fill="#212121" text-anchor="middle">{code}</text>')
          if note:
            body.append(f'<text x="{nx + NW/2:.0f}" y="{ny + 31:.0f}" font-size="9" '
                        f'fill="#595959" text-anchor="middle">{note}</text>')
          nodes.append({'cx': nx + NW / 2, 'top': ny, 'bottom': ny + NH})
      node_rows.append(nodes)
      y += depth * (NH + 6) + VGAP
    height = y + PAD

    # Orthogonal arrows: every node in group i -> every node in group i+1
    for i in range(len(node_rows) - 1):
      for a in node_rows[i]:
        for b in node_rows[i + 1]:
          mid = b['top'] - 12
          body.append(f'<polyline points="{a["cx"]:.0f},{a["bottom"]:.0f} '
                      f'{a["cx"]:.0f},{mid:.0f} {b["cx"]:.0f},{mid:.0f} '
                      f'{b["cx"]:.0f},{b["top"] - 2:.0f}" fill="none" '
                      f'stroke="#666666" stroke-width="1.5" marker-end="url(#arw)"/>')

    # Lanes: separators full height + colored headers with letter-name text
    chrome = []
    for i, d in enumerate(lanes):
      x = i * LW
      code = d.get('dept_code') or '?'
      fill, stroke = self._LANE_COLORS.get(code, ('#F5F5F5', '#616161'))
      chrome.append(f'<rect x="{x}" y="0" width="{LW}" height="{HH}" '
                    f'fill="{fill}" stroke="#BBBBBB" stroke-width="1"/>')
      # Header format: the letter alone on top (prominent), then each word
      # of the department name on its own line beneath it - no truncation.
      # The whole block is VERTICALLY CENTERED within the header band, so
      # short names (Assemble) sit as neatly as long ones (Material Handling).
      words = d['department_name'].split()
      block = 17 + len(words) * 13          # letter line + word lines
      y0 = (HH - block) / 2
      chrome.append(f'<text x="{x + LW/2}" y="{y0 + 14:.0f}" font-size="15" '
                    f'font-weight="bold" fill="{stroke}" text-anchor="middle">'
                    f'{code}</text>')
      for wi, word in enumerate(words):
        chrome.append(f'<text x="{x + LW/2}" y="{y0 + 28 + wi * 13:.0f}" '
                      f'font-size="11" font-weight="bold" fill="{stroke}" '
                      f'text-anchor="middle">{word}</text>')
      chrome.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" '
                    f'stroke="#DDDDDD" stroke-width="1"/>')
    chrome.append(f'<rect x="0" y="0" width="{width}" height="{height}" '
                  f'fill="none" stroke="#BBBBBB" stroke-width="1.5"/>')

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'style="width:100%;font-family:Arial;">'
           '<defs><marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" '
           'markerWidth="6" markerHeight="6" orient="auto">'
           '<path d="M0 0L10 5L0 10z" fill="#666666"/></marker></defs>'
           + ''.join(chrome) + ''.join(body) + '</svg>')
    # Div wrap: Anvil's HTML component sets className on the root element,
    # and SVG roots expose className as READ-ONLY (browser TypeError).
    return '<div>' + svg + '</div>'

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
    Notification(f"{wo_num} released to the floor").show()

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
      c = confirm(f"{wo_num} was not released.\n\nSkip it and continue "
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
            Notification(f"{nxt.get('work_order_number', '')} released "
                         f"(routing reused)").show()
            continue
        alert(f"{nxt.get('work_order_number', '')}: "
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
