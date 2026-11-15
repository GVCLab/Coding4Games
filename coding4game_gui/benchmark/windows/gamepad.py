"""Canonical Xbox controls shared by drivers, config, and recording."""

# Canonical button names → XUSB enum names.
XBOX_BUTTON_MAP = {
    "DPAD_UP": "XUSB_GAMEPAD_DPAD_UP",
    "DPAD_DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "DPAD_LEFT": "XUSB_GAMEPAD_DPAD_LEFT",
    "DPAD_RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
    "START": "XUSB_GAMEPAD_START",
    "BACK": "XUSB_GAMEPAD_BACK",
    "LEFT_SHOULDER": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "RIGHT_SHOULDER": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "GUIDE": "XUSB_GAMEPAD_GUIDE",
    "WEST": "XUSB_GAMEPAD_X",
    "SOUTH": "XUSB_GAMEPAD_A",
    "EAST": "XUSB_GAMEPAD_B",
    "NORTH": "XUSB_GAMEPAD_Y",
    "LEFT_THUMB": "XUSB_GAMEPAD_LEFT_THUMB",
    "RIGHT_THUMB": "XUSB_GAMEPAD_RIGHT_THUMB",
}

BUTTON_ACTION_TOKENS = sorted((*XBOX_BUTTON_MAP, "LEFT_TRIGGER", "RIGHT_TRIGGER"))
BUTTON_TOKEN_SET = frozenset(BUTTON_ACTION_TOKENS)

# Friendly aliases the YAML config can use for buttons.
BUTTON_ALIASES = {
    "A": "SOUTH", "B": "EAST", "X": "WEST", "Y": "NORTH",
    "LB": "LEFT_SHOULDER", "RB": "RIGHT_SHOULDER",
    "LT": "LEFT_TRIGGER", "RT": "RIGHT_TRIGGER",
    "LS": "LEFT_THUMB", "RS": "RIGHT_THUMB",
}


def resolve_button(name: str) -> str | None:
    """Resolve a canonical Xbox control name or its alias."""
    if not isinstance(name, str):
        return None
    token = name.strip().upper()
    token = BUTTON_ALIASES.get(token, token)
    return token if token in BUTTON_TOKEN_SET else None
