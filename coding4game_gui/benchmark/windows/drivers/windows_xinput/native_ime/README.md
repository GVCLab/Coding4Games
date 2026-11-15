# Owned window IMM helper

This host-only x86 component changes the public Windows input-context
association of one explicitly configured game HWND. It uses no game-private
symbols, gameplay calls, input remapping, layout change or capture exemption.

The parent validates the process name, creation time, HWND owner, architecture,
foreground and component hashes. The x86 controller repeats target-generation
checks, installs `WH_CALLWNDPROC` for that one nonzero UI thread, then sends one
registered request with a500ms timeout. The callback calls `ImmAssociateContext`
on the owning thread. It retains the old context in an owned-window property,
verifies the detached state, and can restore that exact handle. It always passes
messages to the next hook. The controller removes the hook before returning.
The host bounds the helper to3s and rejects missing or failed receipts.

`ime_context_mode: disabled` is explicit per-driver startup configuration.
Unspecified games retain the system mode and execute no helper. Preparation runs
after recorded menu playback and final foreground verification, before the
existing hard freeze. Shutdown restores the association after its existing
resume; if an unresponsive or destroyed window cannot be restored, normal
owned-game destruction is still required and removes its context. Layouts,
registry and unrelated windows remain untouched. Text-entry scenarios should
keep the system mode. Only x86 targets are supported by this component.

Rebuild only when no real game, independent test or desktop test is live:

```powershell
python scripts/diagnostics/build_owned_ime_helper.py --zig ABSOLUTE_ZIG_0_13_EXE --cache TASK_COMPILER_CACHE
```

The checked-in manifest pins source and binary bytes. `MINGW_COPYING.txt`
contains the toolchain runtime notice. Software-window and mocked checks do not
replace separate native game acceptance.

Public API contracts:
[IMM thread ownership](https://learn.microsoft.com/en-us/windows/win32/intl/developing-ime-aware-multiple-thread-applications),
[context association](https://learn.microsoft.com/en-us/windows/win32/api/imm/nf-imm-immassociatecontext),
[thread-specific hooks](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexa).
