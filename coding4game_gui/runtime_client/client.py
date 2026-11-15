"""Standalone stdlib bridge client and minimal newline-JSON MCP adapter."""
from __future__ import annotations

import argparse
import base64
import copy
import itertools
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor


def _tool(name, description, properties=None, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {},
        "required": list(required), "additionalProperties": False}}


GUI_KEYS = ("UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACKSPACE", "SPACE", "TAB", "ESC",
            "SHIFT", "CTRL", *tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), *tuple("0123456789"))
_X = {"type": "integer", "minimum": 0, "maximum": 1279,
      "description": "Horizontal screenshot pixel coordinate measured left to right; 0 is the left edge."}
_Y = {"type": "integer", "minimum": 0, "maximum": 767,
      "description": "Vertical screenshot pixel coordinate measured top to bottom; 0 is the top edge."}
_POINT = {"type": "object", "properties": {"x": _X, "y": _Y},
          "required": ["x", "y"], "additionalProperties": False}


def _gui_variant(kind, fields):
    properties = {"type": {"type": "string", "enum": [kind]},
                  "tics": {"type": "integer", "minimum": 1, "maximum": 1000}, **fields}
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


GUI_ACTION_SCHEMA = {"oneOf": [
    _gui_variant("click", {"button": {"type": "string", "enum": ["left", "right", "middle"]}, "x": _X, "y": _Y}),
    _gui_variant("double_click", {"x": _X, "y": _Y}),
    _gui_variant("move", {"x": _X, "y": _Y}),
    _gui_variant("drag", {"path": {"type": "array", "items": _POINT, "minItems": 2, "maxItems": 2}}),
    _gui_variant("scroll", {"x": _X, "y": _Y,
        "scroll_x": {"type": "integer", "minimum": -10, "maximum": 10},
        "scroll_y": {"type": "integer", "minimum": -10, "maximum": 10}}),
    _gui_variant("keypress", {"keys": {"type": "array", "minItems": 1, "uniqueItems": True,
                                      "items": {"type": "string", "enum": list(GUI_KEYS)}}}),
    _gui_variant("wait", {}),
]}
STEP_TOOL = _tool("game.step",
    "In keyboard/mouse mode, execute ordered structured actions and return one final observation. "
    "This is a CUA-style subset, not the native computer API. Coordinates are 1280x768 screenshot pixels. "
    "Each action has integer tics from 1 to the selected scenario cap and remains a resume/input/release/freeze pulse. "
    "The task prompt and bench.status may report a cap below the global 1000-tic schema capability. Long holds are "
    "continuous without hidden represses; actions over the scenario cap are rejected. A click is injected once; "
    "its tics cover delivery and settling, not mouse-down duration. Keypress holds keys "
    "together for those tics; wait advances without input. Drag is a two-point straight line. "
    "Scroll uses wheel steps, NOT pixels: positive x is right, positive y is down; at least one must be nonzero. "
    "Click/double-click release the button and leave the pointer at the target; "
    "move, drag and scroll also leave it at the target. Use move explicitly to move away. "
    "The host validates the whole batch before input and stops remaining actions on limits or termination. "
    "Scores remain null during gameplay and settle after the episode ends. "
    "Registered terminal screens are checked only after each action finishes; inactivity is not terminal. "
    "Inspect the batch receipt before continuing; errors may retain an executed prefix, so never blindly replay. "
    "Usage is cumulative running seconds times the tic rate, rounded to the nearest integer with halves up. "
    "Each actual_tics is the change in this counter; remaining budgets use integer tics. The timing receipt is a host "
    "pause-boundary estimate, not an internal engine clock; control transitions are reported separately. "
    "A final-action overrun of at most two tics is valid but still charged; it permits no further action. "
    "In gamepad mode, use controls plus tics. Keyboard/mouse mode requires structured actions except "
    "RMOVE[dx,dy] and CLICK_CURRENT; each pointer token must be the sole control in its call. "
    "RMOVE uses signed mouse-device units from -1000 to 1000, not both zero. "
    "Never mix actions with controls/tics. Stepped episodes only; "
    "game.observe takes an observation without advancing a frozen game.",
    {"actions": {"type": "array", "minItems": 1, "items": GUI_ACTION_SCHEMA},
     "controls": {"type": "array", "items": {"type": "string"}},
     "tics": {"type": "integer", "minimum": 1, "maximum": 1000}, "episode_id": {"type": "string"}})
# Keep a plain object at the root: Claude Code drops tools with top-level oneOf.
# OpenSession._validate enforces actions OR controls+tics before any game input.


def _validate_gui(value, schema, path="action"):
    """Validate the closed, stdlib-only GUI schema on the trusted host too."""
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "integer": type(value) is int, "string": isinstance(value, str)}
    if not valid.get(kind, False):
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"unsupported {path}: {value!r}")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise ValueError(f"{path} requires exactly {schema['required']}")
        for key, item in value.items():
            _validate_gui(item, schema["properties"][key], f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"invalid {path} length")
        for index, item in enumerate(value):
            _validate_gui(item, schema["items"], f"{path}[{index}]")
        if schema.get("uniqueItems") and len(set(value)) != len(value):
            raise ValueError(f"{path} must not contain duplicates")
    elif kind == "integer":
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError(f"{path} is out of range")


def gui_action_plan(actions):
    """Lower CUA-style actions to existing pulses; no input or budget side effects.

    Scroll amounts are wheel steps, not pixels; positive y scrolls DOWN.
    Drag accepts exactly two points and keeps the existing straight-line gesture.
    """
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a nonempty ordered list")
    plan = []
    for index, action in enumerate(actions):
        kind = action.get("type") if isinstance(action, dict) else None
        shape = next((s for s in GUI_ACTION_SCHEMA["oneOf"]
                      if s["properties"]["type"]["enum"] == [kind]), None)
        if shape is None:
            raise ValueError(f"unsupported action type at index {index}")
        _validate_gui(action, shape, f"actions[{index}]")
        if kind == "wait": controls = ["NEUTRAL"]
        elif kind == "keypress":
            if "ESC" in action["keys"] and len(action["keys"]) != 1:
                raise ValueError("ESC must be the only key in its action")
            controls = [f"KEY[{key}]" for key in action["keys"]]
        elif kind == "drag":
            start, end = action["path"]
            controls = [f"DRAG[{start['x']},{start['y']},{end['x']},{end['y']}]"]
        elif kind == "scroll":
            dx, dy = action["scroll_x"], action["scroll_y"]
            if dx == dy == 0: raise ValueError("scroll must contain at least one nonzero wheel step")
            controls = [f"SCROLL[{action['x']},{action['y']},{dx},{-dy}]"]
        elif kind == "click":
            token = {"left": "CLICK", "right": "RCLICK",
                     "middle": "MCLICK"}[action["button"]]
            controls = [f"{token}[{action['x']},{action['y']}]"]
        else:
            token = {"double_click": "DOUBLE_CLICK", "move": "MOVE"}[kind]
            controls = [f"{token}[{action['x']},{action['y']}]"]
        plan.append((controls, action["tics"]))
    return plan


SOURCE_PATH_SCHEMA = {"type": "string", "description":
    "A dedicated source directory inside the development workspace, for example my_program. "
    "Entrypoint argv is relative to this directory. Do not submit the workspace root, "
    "CLI configuration/runtime directories, credentials, virtual environments, or symlinks."}


TOOLS = [
    _tool("game.start", "Start a fresh development episode.", {
        "max_tics": {"type": ["integer", "null"], "minimum": 1},
        "mode": {"enum": ["stepped"]}}, ("mode",)),
    _tool("game.observe", "Capture without advancing game time. Python Client.call returns "
          "observation={mimeType: 'image/png', data: '<base64 PNG>'}; decode data with "
          "c4g_client.observation_bytes(result) to obtain encoded image bytes. "
          "Python results have no MCP content blocks. The MCP adapter presents the image as image content.", {
        "episode_id": {"type": "string"}}),
    STEP_TOOL,
    _tool("game.stop", "Stop and close the current episode.", {
        "episode_id": {"type": "string"}}),
    _tool("bench.status", "Read development budget and session status."),
    _tool("bench.replay", "Read recorded episode evidence without playing.", {
        "uid": {"type": "string"}, "start": {"type": ["integer", "string"]},
        "end": {"type": ["integer", "string"]},
        "every": {"type": "integer", "minimum": 1}}, ("uid", "start", "end")),
    _tool("submission.save", "Save an immutable executable program.", {
        "path": SOURCE_PATH_SCHEMA,
        "entrypoint": {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "minItems": 1,
                         "items": {"type": "string", "minLength": 1}},
                "mode": {"type": "string", "enum": ["stepped"]},
                "runtime": {"type": "string", "enum": ["python"]},
            },
            "required": ["argv", "mode", "runtime"],
            "additionalProperties": False,
        },
        "description": {"type": "string"},
        "dependencies": {"type": ["object", "null"]}}, ("path", "entrypoint")),
    _tool("submission.run", "Run one saved program in the development box.", {
        "id": {"type": "string"},
        "max_tics": {"type": ["integer", "null"], "minimum": 1}}, ("id",)),
    _tool("submission.finish", "Select the final saved program at any time; unused game tics remain unspent.", {
        "id": {"type": "string"}}, ("id",)),
]
FINAL_NAMES = {"game.observe", "game.step", "game.stop", "bench.status"}
GAME_CONTEXT_TOOLS = {"game.observe", "game.step", "game.stop"}
_PREFIX = uuid.uuid4().hex
_COUNTER = itertools.count(1)


class BridgeError(RuntimeError):
    pass


def observation_bytes(result: dict) -> bytes | None:
    """Decode a Python Client result; an ended result may have no image."""
    if not isinstance(result, dict):
        raise BridgeError("Expected a Python Client result dictionary")
    observation = result.get("observation")
    if observation is None and result.get("done") is True:
        return None
    if not isinstance(observation, dict) or not isinstance(observation.get("data"), str):
        raise BridgeError("Expected Client result['observation']['data']; MCP content is a different format")
    if not str(observation.get("mimeType", "")).startswith("image/"):
        raise BridgeError("Observation is missing an image mimeType")
    try:
        raw = base64.b64decode(observation["data"], validate=True)
    except (ValueError, TypeError) as exc:
        raise BridgeError("Observation data is not valid base64") from exc
    if not raw:
        raise BridgeError("Observation image is empty")
    return raw


class Client:
    def __init__(self, url: str, token: str, *, timeout: float = 3600):
        if not url or not token:
            raise ValueError("bridge URL and token are required")
        self.url, self.token, self.timeout = url, token, timeout
        # This is the dedicated host-game channel, never a system-proxy route.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("C4G_BRIDGE_URL", ""),
                   os.environ.get("C4G_BRIDGE_TOKEN", ""))

    def call(self, tool: str, **arguments):
        episode_id = os.environ.get("C4G_EPISODE_ID")
        if tool in GAME_CONTEXT_TOOLS and episode_id and "episode_id" not in arguments:
            arguments["episode_id"] = episode_id
        payload = {"request_id": f"{_PREFIX}-{next(_COUNTER)}",
                   "tool": tool, "arguments": arguments}
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=raw, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                envelope = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                envelope = json.load(exc)
            except Exception:
                raise BridgeError(f"bridge HTTP error {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise BridgeError(f"bridge transport failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
            raise BridgeError(str(error.get("message") or error.get("code") or "bridge call failed"))
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise BridgeError("bridge returned a malformed result")
        return result


MAX_MCP_CONTENT_BYTES = 2 * 1024 * 1024


CLAUDE_VIEWPORT = (768, 461)
GEMINI_VIEWPORT = (1000, 1000)


def _viewport_tools(size=CLAUDE_VIEWPORT):
    """Describe the shared experiment viewport for MCP discovery."""
    tools = copy.deepcopy(TOOLS)
    step = next(tool for tool in tools if tool["name"] == "game.step")
    step["description"] = step["description"].replace("1280x768", f"{size[0]}x{size[1]}")
    step["description"] += (
        " The host maps pointer coordinates to game pixels automatically. "
        f"MCP and Python Client.call both use {size[0]}x{size[1]}; do not scale them yourself.")
    if tuple(size) == GEMINI_VIEWPORT:
        step["description"] += (
            " Pointer actions require named x/y fields: x is horizontal (left to right) and y is "
            "vertical (top to bottom). Gemini image grounding may express points as [y,x]; convert "
            "that convention before calling this tool and never copy [y,x] into the named x/y fields.")

    def visit(node):
        if isinstance(node, dict):
            for key, value in node.get("properties", {}).items():
                if key in {"x", "y"}:
                    value["maximum"] = size[key == "y"] - 1
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(step["inputSchema"])
    return tools


def _viewport_arguments(name, arguments, size=CLAUDE_VIEWPORT):
    """Validate a whole experiment action batch before mapping coordinates."""
    if name != "game.step" or "actions" not in arguments:
        return arguments
    actions = arguments["actions"]
    if not isinstance(actions, list) or not actions:
        raise ValueError("actions must be a nonempty ordered list")
    shapes = next(t for t in _viewport_tools(size) if t["name"] == name)[
        "inputSchema"]["properties"]["actions"]["items"]["oneOf"]
    mapped = copy.deepcopy(arguments)
    for index, action in enumerate(mapped["actions"]):
        kind = action.get("type") if isinstance(action, dict) else None
        shape = next((s for s in shapes if s["properties"]["type"]["enum"] == [kind]), None)
        if shape is None:
            raise ValueError(f"unsupported action type at index {index}")
        _validate_gui(action, shape, f"actions[{index}]")
        points = action["path"] if kind == "drag" else [action] if "x" in action else []
        for point in points:
            # Match the endpoint-preserving canonical policy coordinate mapping.
            point["x"] = round(point["x"] * 1279 / (size[0] - 1))
            point["y"] = round(point["y"] * 767 / (size[1] - 1))
    return mapped


def _mcp_content(result: dict, *, claude_viewport=False, viewport=None) -> list[dict]:
    content = _build_mcp_content(result)
    viewport = CLAUDE_VIEWPORT if claude_viewport else viewport
    if viewport:
        content = copy.deepcopy(content)
        images = [block for block in content if block.get("type") == "image"]
        if images:
            import cv2
            import numpy as np
        for block in images:
            frame = cv2.imdecode(np.frombuffer(base64.b64decode(block["data"]),
                                              dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise BridgeError("could not decode viewport image")
            frame = cv2.resize(frame, viewport, interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                raise BridgeError("could not encode viewport image")
            block["mimeType"] = "image/jpeg"
            block["data"] = base64.b64encode(encoded.tobytes()).decode("ascii")
        content.insert(0, {"type": "text", "text":
            f"Experiment screenshot/point coordinates: {viewport[0]}x{viewport[1]}. MCP and Python "
            "Client.call use these same pixels, including submitted programs. "
            "The host maps pointer positions automatically; do not compensate."
            + (" Pointer actions use named fields: x is horizontal (left to right) and y is vertical "
               "(top to bottom). Gemini image grounding may express points as [y,x]; convert them "
               "before calling game.step and never copy [y,x] into x/y."
               if tuple(viewport) == GEMINI_VIEWPORT else "")})
    if len(json.dumps(content, ensure_ascii=False).encode('utf-8')) > MAX_MCP_CONTENT_BYTES:
        raise BridgeError('Image response is too large; request fewer replay frames or a shorter window.')
    return content


def _build_mcp_content(result: dict) -> list[dict]:
    result = dict(result)
    if 'content' in result and set(result) <= {'content', 'isError'}:
        if result.get('isError'):
            raise BridgeError('Upstream tool returned an error')
        content = result['content']
        if not isinstance(content, list) or any(
                not isinstance(item, dict) or item.get('type') not in {'text', 'image'}
                for item in content):
            raise BridgeError('Malformed MCP content')
        return content
    observation = result.pop("observation", None)
    frames = result.pop("frames", None)
    if frames is not None:
        if not isinstance(frames, list) or len(frames) > 40:
            raise BridgeError("malformed replay frames")
        content = _mcp_content(result)
        for frame in frames:
            if not isinstance(frame, dict):
                raise BridgeError("malformed replay frame")
            content.extend(_mcp_content(frame))
        return content
    content = [{"type": "text", "text": json.dumps(
        result, ensure_ascii=False, separators=(",", ":"))}]
    if observation is not None:
        if not isinstance(observation, dict) or not isinstance(
                observation.get("data"), str):
            raise BridgeError("malformed image observation")
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise BridgeError("cv2 and numpy are required for policy image encoding") from exc
        try:
            source = observation_bytes({"observation": observation})
            frame = cv2.imdecode(np.frombuffer(source, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception as exc:
            raise BridgeError("could not decode observation image") from exc
        if frame is None:
            raise BridgeError("could not decode observation image")
        frame = cv2.resize(frame, tuple(result.get("observation_size", (1280, 768))),
                           interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise BridgeError("could not encode policy observation")
        content.append({"type": "image", "mimeType": "image/jpeg",
                        "data": base64.b64encode(encoded.tobytes()).decode("ascii")})
    return content


def _reply(request_id, *, result=None, error=None):
    value = {"jsonrpc": "2.0", "id": request_id}
    value["result" if error is None else "error"] = result if error is None else error
    return value


def mcp_main(*, stdin=None, stdout=None, claude_viewport=False, viewport=None) -> int:
    viewport = CLAUDE_VIEWPORT if claude_viewport else viewport
    if viewport is not None and tuple(viewport) not in {CLAUDE_VIEWPORT, GEMINI_VIEWPORT}:
        raise ValueError("unsupported experiment viewport")
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    # MCP stdio is UTF-8, regardless of the Windows locale or pipe defaults.
    for stream in (stdin, stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    output_lock = threading.Lock()
    run_slot = threading.BoundedSemaphore(1)

    def write(value):
        raw = json.dumps(value, ensure_ascii=False) + "\n"
        with output_lock:
            stdout.write(raw)
            stdout.flush()

    def handle(line):
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            request_id, method = request.get("id"), request.get("method")
            if method == "notifications/initialized":
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "c4g-game-bridge", "version": "1"}}
            elif method == "tools/list":
                available = _viewport_tools(viewport) if viewport else TOOLS
                tools = (available if os.environ.get("C4G_BRIDGE_PHASE", "development")
                         == "development" else [item for item in available
                                                if item["name"] in FINAL_NAMES])
                result = {"tools": tools}
            elif method == "tools/call":
                params = request.get("params") or {}
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    raise ValueError("tools/call requires a tool name")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                client = Client.from_env()
                result = {"content": _mcp_content(client.call(params["name"], **arguments),
                                                 viewport=viewport),
                          "isError": False}
            else:
                raise ValueError("method not supported")
            if request_id is not None:
                write(_reply(request_id, result=result))
        except Exception as exc:
            if request_id is not None:
                error = {"code": -32603, "message": f"{type(exc).__name__}: {exc}"[:1000]}
                write(_reply(request_id, error=error))

    def run(line):
        try:
            handle(line)
        finally:
            run_slot.release()

    # Only the long candidate evaluation runs off the reader thread. Other
    # operations retain their order, and host locks still serialize gameplay.
    # Reject overlapping runs instead of silently queueing future game starts.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-program") as worker:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                is_run = (isinstance(request, dict) and request.get("method") == "tools/call"
                          and isinstance(request.get("params"), dict)
                          and request["params"].get("name") == "submission.run")
            except ValueError:
                is_run = False
            if is_run:
                if run_slot.acquire(blocking=False):
                    worker.submit(run, line)
                elif request.get("id") is not None:
                    write(_reply(request["id"], error={"code": -32603,
                        "message": "A submission.run call is already pending; use game.stop or bench.status."}))
            else:
                handle(line)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", action="store_true", required=True)
    parser.add_argument("--claude-viewport", action="store_true",
                        help="Use a 768x461 MCP viewport with canonical pointer mapping.")
    parser.add_argument("--viewport", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"),
                        help="Use the experiment image and pointer dimensions.")
    args = parser.parse_args()
    if args.claude_viewport and args.viewport:
        parser.error("select only one viewport option")
    raise SystemExit(mcp_main(claude_viewport=args.claude_viewport, viewport=args.viewport))


if __name__ == "__main__":
    main()
