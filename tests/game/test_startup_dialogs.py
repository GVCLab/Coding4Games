"""The optional download handler must not accept unrelated native dialogs."""

import pytest

from coding4game_gui.benchmark.windows import startup_dialogs as dialogs


OFFER = (
    "NVIDIA图形驱动器的安装版本在D3D11中存在已知问题。\n"
    "是否要访问以下URL下载驱动？\n"
    "https://www.nvidia.com/en-us/geforce/drivers/"
)


@pytest.mark.parametrize("api", ["D3D11", "D3D12"])
def test_only_the_observed_download_question_matches(api):
    assert dialogs.is_nvidia_driver_download_offer(OFFER.replace("D3D11", api))


@pytest.mark.parametrize("text", [
    "", "NVIDIA driver installation requires administrator approval",
    OFFER.replace("是否要访问以下URL下载驱动", "是否继续安装驱动"),
    OFFER.replace("https://www.nvidia.com/en-us/geforce/drivers/", "https://example.org/"),
])
def test_other_warnings_and_installation_prompts_are_not_handled(text):
    assert not dialogs.is_nvidia_driver_download_offer(text)


@pytest.mark.parametrize("process,window_class,button_text,should_decline", [
    ("Palworld-Win64-Shipping.exe", "#32770", "否(&N)", True),
    ("Other.exe", "#32770", "否(&N)", False),
    ("Palworld-Win64-Shipping.exe", "UnrealWindow", "否(&N)", False),
])
def test_declines_only_the_owned_native_offer(
        monkeypatch, process, window_class, button_text, should_decline):
    sent = []
    monkeypatch.setattr(dialogs.win32gui, "GetClassName", lambda _h: window_class)
    monkeypatch.setattr(dialogs.win32gui, "EnumChildWindows", lambda _h, cb, data: cb(2, data))
    monkeypatch.setattr(dialogs.win32gui, "GetWindowText", lambda h: OFFER if h == 2 else button_text)
    monkeypatch.setattr(dialogs.win32gui, "GetDlgItem", lambda _h, ident: 3)
    monkeypatch.setattr(dialogs.win32gui, "PostMessage", lambda *args: sent.append(args))
    monkeypatch.setattr(dialogs, "emit", lambda *args, **kwargs: None)
    assert dialogs.decline_palworld_driver_download(process, 1) is should_decline
    assert bool(sent) is should_decline
    if should_decline:
        assert sent == [(1, dialogs.win32con.WM_COMMAND, dialogs.win32con.IDNO, 3)]
