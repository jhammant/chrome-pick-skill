---
name: chrome-pick
description: Select the user's Chrome browser in Codex when they ask which browser is in use or report that the wrong browser was chosen.
---

# Chrome selection in Codex

Use the installed Chrome plugin's skill and supported browser selector. Read
that skill before connecting. An explicit Chrome or machine choice remains in
force for the task. Reuse an existing valid connection; obtain a fresh tab when
only the tab has closed.

Claude-in-Chrome device IDs, global `select_browser`, cached browser labels and
the network-beacon procedure are specific to Claude's extension. Do not use
them for Codex or assume matching labels identify the same browser.

If the requested browser cannot be connected, use the Chrome plugin's
troubleshooting flow and describe the limitation. Do not silently substitute
another browser or machine. This adapter does not implement the Claude skill's
multi-machine beacon feature.
