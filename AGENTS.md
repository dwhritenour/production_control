# Production Control v1.0 (the LIVE production app) — agent rules (any editor)

These rules bind every coding agent that works in this repository, whichever editor
hosts it (Cursor, VS Code with Copilot / Cline / Claude Code / Codex, or a terminal
agent). This file is the single source; `.cursor/rules/anvil.mdc`, `CLAUDE.md` and
`.github/copilot-instructions.md` only point here. Edit the rules HERE.

This repository is an **Anvil.works** application that is two-way synced with the
Anvil Cloud Editor through GitHub. Pushing to `origin master` deploys to the
Anvil *editing* branch. The `published` branch is what shop-floor users run.

**This is the production app (repo `dwhritenour/production_control`).** Until the
in-house move, Dave also pastes files into the Anvil editor by hand; Anvil commits
those as "Edited <form>" - always `git pull --ff-only` before touching anything.

## 1. Git workflow (non-negotiable)

- Work directly on `master`. Do not create feature branches unless explicitly asked.
- **Before editing anything, run `git pull origin master`.** Dave makes visual
  changes in the Anvil designer; those arrive as commits named "Edited <form>".
  Editing a stale file silently reverts his designer work.
- After completing the requested change: `git add -A`, commit with a clear,
  specific message (what changed and why, one line), then `git push origin master`.
  `SPEC.md` is gitignored and must never be committed; do not force-add it.
- **Never modify, push to, or reset the `published` branch** unless the request
  explicitly says to release. A release is:
  `git push origin master:published --force-with-lease` (never bare `--force`).
- Before any multi-file refactor, create and push a tag:
  `backup-YYYY-MM-DD-<short-reason>`.
- To undo the last pushed commit use `git revert HEAD --no-edit` then push.
  Do not rewrite history on `master`.
- If `git pull` reports conflicts in a `form_template.yaml`, STOP and report.
  Do not resolve designer-file conflicts by guessing.
- **Never discard the working tree.** No `git reset --hard`, `git checkout -- .`,
  `git restore`, `git stash` or `git clean` unless the request says so in those
  words. Cowork's finished edits live UNCOMMITTED in the working tree until you
  commit them, and `SPEC.md` is gitignored, so `git clean -x` deletes the very
  instructions you are following. (2026-10-19: a "recovery" clean wiped three
  edited files and the spec.) If the tree looks wrong, STOP, paste `git status`,
  and report.
- If a command hangs (most often `git pull`/`push` waiting on a GitHub sign-in
  that only a human can complete), stop and report it. Do not retry in a loop
  and do not "fix" it by resetting anything.

## 2. Repository layout and what you may touch

- `client_code/<FormName>/__init__.py` — form logic (Python, runs in browser via Skulpt).
- `client_code/<FormName>/form_template.yaml` — designer layout. **Generated and
  edited by the Anvil designer.** Edit only when the task explicitly calls for a
  layout change, and then preserve exact structure, indentation, property names
  and component ordering. Never reformat, reorder, or "clean up" YAML.
- `client_code/<FormName>/form_template.html` — custom-HTML forms (a few exist:
  RoutingStatusRow, RoutingStepRow*, grid_pager). Safe to edit.
- `server_code/*.py` — server modules (Python 3, pymssql to the Nexus SQL Server).
  Freely editable. This is where SQL lives.
- `theme/assets/theme.css`, `theme/parameters.yaml` — styling. Editable; see §4.
- `anvil.yaml` — app config, dependencies, server config. Do not edit unless asked.
- `.anvil_editor.yaml`, root `__init__.py` — Anvil boilerplate. Never touch.
- Line endings: files are CRLF on disk, LF in git (`core.autocrlf=true`). Do not
  convert line endings and do not commit whole-file reformatting.

## 3. Anvil client-code constraints (these have each cost us a round-trip)

- Anvil **Labels do not emit a `click` event.** Use a Link or Button for anything
  clickable; `set_event_handler('click', ...)` on a Label silently does nothing.
- Label, CheckBox and FlowPanel have **no `width` property.** Column geometry
  comes from DataGrid columns, never from component widths or CSS roles.
- `role` only accepts names defined in `theme/parameters.yaml`. Do not invent roles.
- `datetime.strptime` suspends in client code — never call it inside a `sorted()`
  key function or any non-suspendable context.
- Server calls: `anvil.server.call('fn', ...)`; server functions are decorated
  `@anvil.server.callable`. Keep heavy work (SQL, PDF) on the server.
- Anvil servers run UTC. Any user-facing timestamp must go through the existing
  `_now_eastern()` helper pattern.
- Search-box style TextBoxes use `placeholder`, not `text`, for the hint.
- A client form (package under `client_code/`) and a server module must never share
  a name: on the server the form package shadows the module (`Cannot import Form
  templates on the server`). Suffix the form (`routing_desk_form`), never the module.
- FlowPanel ignores `container:width` / per-item widths. Fixed column geometry in a
  custom-HTML row comes from a theme role whose CSS makes the panel a grid (see
  `rd-grid` in theme.css), or from a DataGrid.
- Top-level children of a page's root ColumnPanel need `container:full_width_row="true"`
  or Anvil caps them at 970px and centres them.

## 4. House UI pattern (locked — do not deviate)

- Template is **Material Design 3**, stock components everywhere.
- Any tabular, multi-row layout is a **DataGrid**, built *designer-native*:
  columns with explicit ids and titles defined in the designer, `rows_per_page`
  set in the designer, row template is a classic designer form on a
  `DataRowPanel` (never a custom-HTML row), every cell is a designer component,
  text cells are Labels with **data bindings** (`self.item['x_disp']`), with
  display keys injected by the parent form.
- **No runtime component construction.** If it can be a designer property or a
  data binding, it must be. Code does wiring + render (text, colors, visibility).
- **Exception (2026-10-15):** custom-HTML forms (`form_template.html`) are
  permitted where a SPEC supplies the form's HTML and `__init__.py` verbatim
  (as with the existing routing_status, routing_editor, grid_pager and *Row
  forms). Copy those files exactly; the DataGrid / no-runtime-construction rules
  above apply to new designer-built grids, not to a SPEC-supplied form.
- Code-referenced component names are load-bearing; keep exact names. Do not
  rename components, forms, or server functions unless the task says so.
- Colors are set in code or theme.css with exact hex values, not via the
  designer color picker. When you set a color, note the hex and how it reads.

## 5. Data and SQL

- The database is **Nexus** (SQL Server, T-SQL). Server modules query it with
  parameterized pymssql calls. Never build SQL by string formatting user input.
- Schema changes (DDL) are **not** done from this repo. If a change needs a new
  column/table/proc, stop and report: "needs migration" with the DDL you would
  propose. Dave applies migrations through the numbered `migrations/` process.
- `work_orders` table: EasyMorph reloads the e21-mirrored columns hourly; only
  app-owned columns are safe to write from the app.

## 6. How to work a request

1. `git pull origin master`.
2. If a `SPEC.md` is referenced, read it fully before touching code. Implement
   it verbatim — no renaming, reformatting, or "improvements" beyond the spec.
   If a reference in the spec does not resolve (missing component, function,
   column), **report it; do not work around it.**
3. Make the change. Run `python -m py_compile` on every edited `.py` file.
   On Windows, if `python` is the Microsoft Store stub, use `py -3 -m py_compile`
   or the full interpreter path; never skip the check and "verify by reading".
   When a SPEC asks for a CRLF check and `file` is not on the PATH, use
   PowerShell: `(Get-Content -Raw <path>) -match "\r\n"` must print `True`
   (Git for Windows also ships `file.exe` under its `usr\bin`).
4. `git add -A`, commit, `git push origin master`.
5. Reply with: files changed, the commit hash, anything unresolved, and the
   manual smoke test Dave should run in the Anvil editor (green Run button).
