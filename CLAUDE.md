# Working on this repository

## GUI design

`design/GUI.figcraft` is the design of the PySide6 GUI (`sdctoolbox/gui/`). Open it with FigCraft.

- **Every window, dialog and popup the app can open has a frame in the design, in light and
  dark mode.** When code adds, removes or changes one, update the design in the same piece of
  work. The frame names say what they are (`Main window / …`, `Dialog / …`, `Window / …`,
  `popup …`).
- Draft UI changes before coding them: copy the current page (named "Planned UI" or what the
  user asks for) and edit only the copy. The user approves the draft before code changes.
- Keep the two modes identical except for colours, and every place a changed element appears
  consistent (for example, a button that appears in two windows changes in both).
- There is one main window per tab: `Main window / My device` and `Main window / Network`.
  Each shows every section expanded with demo content, and is as tall as its content.
- Keep the Dialogs section ordered and aligned: app-level windows and menus first, then the
  data source, alarm and action dialogs, top-aligned with even gaps, all inside the section.

### Message boxes

Message boxes are standard `QMessageBox`es and are not drawn in the design. These exist:

| Kind | Title | Where |
| --- | --- | --- |
| Question (Yes/No) | Replace this device? | importing a config or loading a preset |
| Question (Yes/No) | Remove data source | removing a metric or its card |
| Question (Yes/No) | Remove alarm | removing an alarm |
| Information | Nothing to watch | New alarm with no data source |
| Information | Nothing to act on | New action with no data source |
| Warning | Could not import, Could not export | File menu |
| Warning | Could not create data source / alarm / action, Could not remove | My device |
| Warning | Outside the allowed range, Invalid value, Could not set value, Could not change remote control | My device values |
| Warning | Could not change the alarm, Could not acknowledge, Could not stop latched signals, Could not delegate | My device alarms |
| Warning | Could not run the action, Could not change the contexts | My device |
| Warning | Could not change the fault | My device Faults section (Advanced mode) |
| Warning | Could not add connection, Could not close connection | Network tabs |
| Warning | Could not load codes | Pick an MDC code (Load codes.csv…, or a remembered file that no longer loads) |
| Warning | Could not start the MCP server | startup with `--mcp` when the port is taken |

Native file pickers (Import config, Export config, Choose a config file, PEM file, certificate
folder, Choose codes.csv) are drawn by the operating system and are not in the design either.
Keep this list current when message boxes change.
