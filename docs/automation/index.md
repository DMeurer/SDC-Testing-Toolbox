---
title: Automation
nav_order: 4
has_children: true
---

# Automation

Three ways to drive the toolbox without clicking, from the most to the least capable:

| Way | Good for | Covers |
|---|---|---|
| [MCP for agents](mcp.md) | letting an AI agent such as Claude Code build devices, check them, connect and test | everything the window does, with or without a window |
| [Text console](console.md) | scripting a demo, a terminal session, a quick check | data sources, alarms, actions, faults, MDIB check, several connections, Device test, messages, and any MCP tool |
| [Python](python-api.md) | your own tests and tools | the provider and consumer services the window is built on |

All three use the same code as the window, so a device built from a script behaves exactly like
one built by hand.
