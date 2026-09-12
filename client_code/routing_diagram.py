"""
routing_diagram — shared swim-lane routing diagram (client module).

2026-09-12: lifted out of sku_details so the same picture can be shown from
the work_orders grid (Routing column -> 'View' link -> popup) without a
second copy of the drawing code. sku_details._build_svg now delegates here,
so the Routing Library and the grid popup can never drift apart.

Public surface
--------------
  steps_from_rows(rows)                 DB rows -> working list
  step_codes(steps, dept_codes)         A01.01 codes for a working list
  build_routing_svg(departments, steps, dept_codes, statuses=None)
  show_routing_popup(work_order_id, work_order_number='', part_number='')

Working list shape (same as sku_details.self._steps):
  {'seq': int, 'department_id': int|None, 'step_notes': str}
Steps sharing a seq run in PARALLEL.

Status overlay (grid popup only): when `statuses` is passed - a list
parallel to `steps` holding pc_wo_routing_steps.status - Complete nodes get
a check mark, In Progress a heavier stroke, Skipped a dashed outline.
sku_details passes nothing, so its diagram is byte-for-byte what it was.

UI standard note: this is the one Routing Diagram HTML/CSS exemption
(DECISION 2026-08-24) - an SVG is the only way to draw lanes + arrows.
"""
from anvil import *
import anvil.server


# Lane colors keyed by dept_code: (node fill, stroke/header text).
# Identity is carried by the letter + header TEXT - color is decoration.
LANE_COLORS = {
  'A': ('#FCE4E4', '#C62828'), 'B': ('#FCE4F1', '#AD1457'),
  'C': ('#EDE7F6', '#6A1B9A'), 'D': ('#E8EAF6', '#283593'),
  'E': ('#E3F2FD', '#1565C0'), 'F': ('#E0F2F1', '#00695C'),
  'G': ('#E8F5E9', '#2E7D32'), 'H': ('#FFF3E0', '#EF6C00'),
}

_departments_cache = None


def get_departments_cached():
  """pc_departments (active, sort_order) - fetched once per app load.
  The list is tiny and only changes when a department is added."""
  global _departments_cache
  if _departments_cache is None:
    _departments_cache = anvil.server.call('get_departments')
  return _departments_cache


def steps_from_rows(rows):
  """DB rows (gapped sequence_numbers) -> working list with display
  numbers 1..n; rows sharing a sequence_number share a display number."""
  seqs = sorted({r['sequence_number'] for r in rows})
  disp = {s: i + 1 for i, s in enumerate(seqs)}
  return [{'seq': disp[r['sequence_number']],
           'department_id': r['department_id'],
           'step_notes': (r.get('step_notes') or '')}
          for r in rows]


def step_codes(steps, dept_codes):
  """A01.01 codes for a SORTED working list. Letter = department,
  2-digit = routing step (sequence group), .2-digit = that DEPARTMENT'S
  step count across the WHOLE routing (Dave 2026-07-23: F04.01 then
  F05.02 - Material Handling's first and second steps)."""
  disp = {g: i + 1 for i, g in enumerate(sorted({s['seq'] for s in steps}))}
  counters, codes = {}, []
  for s in steps:
    gd = disp[s['seq']]
    dept = s['department_id']
    counters[dept] = counters.get(dept, 0) + 1
    letter = dept_codes.get(dept, '?')
    codes.append(f"{letter}{gd:02d}.{counters[dept]:02d}")
  return codes


def _esc(text):
  """Minimal escaping for text dropped into SVG (notes are free text)."""
  return (str(text).replace('&', '&amp;').replace('<', '&lt;')
    .replace('>', '&gt;').replace('"', '&quot;'))


def build_routing_svg(departments, steps, dept_codes, statuses=None):
  """Swim-lane routing diagram (Dave's sample, 2026-07-22): one lane per
  department (ALL lanes, always - spatial consistency), flow top to
  bottom, nodes carry A01.01 codes. `steps` must already be sorted by
  seq (callers sort; sku_details does before every render)."""
  lanes = departments                            # sort_order = lane order
  LW, NW, NH, VGAP, PAD = 140, 112, 40, 34, 8
  # Header: big letter on top, then EACH WORD of the department name on
  # its own line (Dave's format, 2026-07-22). Height fits the wordiest name.
  max_words = max((len(d['department_name'].split()) for d in lanes),
                  default=1)
  HH = 24 + max_words * 13 + 5
  width = len(lanes) * LW
  lane_x = {d['department_id']: i for i, d in enumerate(lanes)}

  if not steps:
    return ('<div style="color:#999999;font-style:italic;padding:24px;'
            'font-family:Arial;">Add steps to see the flow</div>')

  codes = step_codes(steps, dept_codes)
  statuses = list(statuses) if statuses else [None] * len(steps)
  disp = {g: i + 1 for i, g in enumerate(sorted({s['seq'] for s in steps}))}
  # group -> {lane_index: [(step, code, status), ...]}
  groups = {}
  for s, c, st in zip(steps, codes, statuses):
    gd = disp[s['seq']]
    li = lane_x.get(s['department_id'], 0)
    groups.setdefault(gd, {}).setdefault(li, []).append((s, c, st))

  body = []
  y = HH + PAD
  node_rows = []                                 # per group: list of node dicts
  for gd in sorted(groups.keys()):
    lanes_here = groups[gd]
    depth = max(len(v) for v in lanes_here.values())
    nodes = []
    for li, entries in lanes_here.items():
      for k, (s, code, st) in enumerate(entries):
        nx = li * LW + (LW - NW) / 2
        ny = y + k * (NH + 6)
        d = lanes[li]
        fill, stroke = LANE_COLORS.get(d.get('dept_code') or '?',
                                       ('#F5F5F5', '#616161'))
        note = (s.get('step_notes') or '').strip()
        if len(note) > 16:
          note = note[:15] + '…'
        # Status overlay (grid popup): decoration only - the word is in
        # the step list under the diagram (color never alone).
        sw, dash, label = 1.5, '', code
        if st == 'Complete':
          label = '✓ ' + code
        elif st == 'In Progress':
          sw = 3
        elif st == 'Skipped':
          dash = ' stroke-dasharray="4 3"'
        body.append(f'<rect x="{nx:.0f}" y="{ny:.0f}" width="{NW}" height="{NH}" '
                    f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{dash}/>')
        body.append(f'<text x="{nx + NW/2:.0f}" y="{ny + 16:.0f}" font-size="12" '
                    f'font-weight="bold" fill="#212121" text-anchor="middle">{label}</text>')
        if note:
          body.append(f'<text x="{nx + NW/2:.0f}" y="{ny + 31:.0f}" font-size="9" '
                      f'fill="#595959" text-anchor="middle">{_esc(note)}</text>')
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
    fill, stroke = LANE_COLORS.get(code, ('#F5F5F5', '#616161'))
    chrome.append(f'<rect x="{x}" y="0" width="{LW}" height="{HH}" '
                  f'fill="{fill}" stroke="#BBBBBB" stroke-width="1"/>')
    # Header: the letter alone on top (prominent), then each word of the
    # department name on its own line beneath it - no truncation. The
    # block is VERTICALLY CENTERED within the header band.
    words = d['department_name'].split()
    block = 17 + len(words) * 13            # letter line + word lines
    y0 = (HH - block) / 2
    chrome.append(f'<text x="{x + LW/2}" y="{y0 + 14:.0f}" font-size="15" '
                  f'font-weight="bold" fill="{stroke}" text-anchor="middle">'
                  f'{code}</text>')
    for wi, word in enumerate(words):
      chrome.append(f'<text x="{x + LW/2}" y="{y0 + 28 + wi * 13:.0f}" '
                    f'font-size="11" font-weight="bold" fill="{stroke}" '
                    f'text-anchor="middle">{_esc(word)}</text>')
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


# =============================================================================
# Grid popup: "how was this work order routed?"
# =============================================================================
_STEP_STATUS_COLORS = {
  'Complete': '#4CAF50', 'In Progress': '#FF9800', 'Ready': '#1565C0',
  'Pending': '#607D8B', 'Skipped': '#9E9E9E',
}


def show_routing_popup(work_order_id, work_order_number='', part_number=''):
  """Large alert with the WO's saved routing as the swim-lane diagram plus
  a one-line-per-step list (code, department, status, notes). Reads the
  routing snapshot via the existing get_wo_routing (routing_dashboard) -
  no new server code. A WO with no saved routing gets a plain message."""
  result = anvil.server.call('get_wo_routing', work_order_id)
  if result.get('status') != 'success':
    alert(f"Error: {result.get('message', 'Unknown error')}")
    return
  rows = result.get('steps') or []
  title = f"Routing — {work_order_number}".strip(' —')

  if not rows:
    alert(f"{work_order_number} has no saved routing yet.\n\n"
          f"Routing is written when the work order is released "
          f"(WO# → Release).", title=title)
    return

  departments = get_departments_cached()
  dept_codes = {d['department_id']: (d.get('dept_code') or '?')
                for d in departments}
  dept_names = {d['department_id']: d['department_name'] for d in departments}

  # Preserve DB order (sequence_number, wo_step_id) - it is already sorted,
  # and steps_from_rows keeps that order, so codes/statuses line up.
  steps = steps_from_rows(rows)
  statuses = [r.get('status') for r in rows]
  codes = step_codes(steps, dept_codes)

  panel = ColumnPanel()
  hdr = f"{work_order_number}"
  if part_number:
    hdr += f"   |   Part {part_number}"
  hdr += f"   |   {len(rows)} step{'s' if len(rows) != 1 else ''}"
  if result.get('all_complete'):
    hdr += "   |   all steps complete"
  panel.add_component(Label(text=hdr, bold=True))

  panel.add_component(HtmlTemplate(
    html=build_routing_svg(departments, steps, dept_codes, statuses)))

  # Step list: the WORD for every status (color never alone).
  for s, code, r in zip(steps, codes, rows):
    st = r.get('status') or ''
    note = (r.get('step_notes') or '').strip()
    line = f"{code}   {dept_names.get(s['department_id'], '?')}"
    if st:
      line += f"   —   {st}"
    if note:
      line += f"   ·   {note}"
    lbl = Label(text=line, font_size=12,
                foreground=_STEP_STATUS_COLORS.get(st, '#333333'))
    try:
      lbl.spacing_above = 'none'
      lbl.spacing_below = 'none'
    except Exception:
      pass                          # runtime without the legacy props
    panel.add_component(lbl)

  alert(content=panel, title=title, large=True, buttons=[("Close", None)])