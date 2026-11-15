/* Change only the addressed window's public IMM association on its UI thread. */
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <imm.h>

static const WCHAR previous_key[] = L"C4G.OwnedImeContext.Previous.v1";
static const WCHAR state_key[] = L"C4G.OwnedImeContext.State.v1";
static const WCHAR message_name[] = L"C4G.OwnedImeContext.Request.v1";

__declspec(dllexport) LRESULT CALLBACK OwnedImeHook(int code, WPARAM w, LPARAM l) {
    if (code >= 0 && l) {
        const CWPSTRUCT *event = (const CWPSTRUCT *)l;
        UINT request = RegisterWindowMessageW(message_name);
        if (request && event->message == request && event->lParam == 0x43473449) {
            DWORD pid = 0;
            DWORD tid = GetWindowThreadProcessId(event->hwnd, &pid);
            if (pid == GetCurrentProcessId() && tid == GetCurrentThreadId()) {
                HANDLE state = GetPropW(event->hwnd, state_key);
                if (event->wParam == 1 && !state) {
                    HIMC context = ImmGetContext(event->hwnd);
                    if (context) ImmReleaseContext(event->hwnd, context);
                    if (context && SetPropW(event->hwnd, previous_key, context)) {
                        HIMC previous = ImmAssociateContext(event->hwnd, NULL);
                        HIMC after = ImmGetContext(event->hwnd);
                        if (after) ImmReleaseContext(event->hwnd, after);
                        if (previous == context && !after) {
                            if (!SetPropW(event->hwnd, state_key, (HANDLE)1)) {
                                ImmAssociateContext(event->hwnd, context);
                                RemovePropW(event->hwnd, previous_key);
                            }
                        } else {
                            ImmAssociateContext(event->hwnd, context);
                            RemovePropW(event->hwnd, previous_key);
                            SetPropW(event->hwnd, state_key, (HANDLE)3);
                        }
                    } else if (!context) {
                        SetPropW(event->hwnd, state_key, (HANDLE)4);
                    }
                } else if (event->wParam == 1 && state == (HANDLE)1) {
                    HIMC current = ImmGetContext(event->hwnd);
                    if (current) {
                        ImmReleaseContext(event->hwnd, current);
                        SetPropW(event->hwnd, state_key, (HANDLE)6);
                    }
                } else if (event->wParam == 2 && state == (HANDLE)1) {
                    HIMC previous = (HIMC)GetPropW(event->hwnd, previous_key);
                    ImmAssociateContext(event->hwnd, previous);
                    HIMC after = ImmGetContext(event->hwnd);
                    if (after) ImmReleaseContext(event->hwnd, after);
                    if (previous && after == previous) {
                        RemovePropW(event->hwnd, previous_key);
                        SetPropW(event->hwnd, state_key, (HANDLE)2);
                    } else {
                        SetPropW(event->hwnd, state_key, (HANDLE)5);
                    }
                }
            }
        }
    }
    return CallNextHookEx(NULL, code, w, l);
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)instance; (void)reason; (void)reserved;
    return TRUE;
}
