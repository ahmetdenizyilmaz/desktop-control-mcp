import re
import time

from flask import Flask, jsonify, request, render_template

import controller

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/screenshot", methods=["POST"])
def screenshot():
    try:
        image = controller.take_screenshot()
        return jsonify({"status": "ok", "image": image})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/move", methods=["POST"])
def move():
    try:
        data = request.get_json(force=True)
        x = int(data["x"])
        y = int(data["y"])
        controller.move_mouse(x, y)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/click", methods=["POST"])
def click():
    try:
        data = request.get_json(force=True)
        x = int(data["x"])
        y = int(data["y"])
        button = data.get("button", "left")
        controller.click_mouse(x, y, button)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/drag", methods=["POST"])
def drag():
    try:
        data = request.get_json(force=True)
        x1 = int(data["x1"])
        y1 = int(data["y1"])
        x2 = int(data["x2"])
        y2 = int(data["y2"])
        button = data.get("button", "left")
        controller.drag_mouse(x1, y1, x2, y2, button)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/keys", methods=["POST"])
def keys():
    try:
        data = request.get_json(force=True)
        text = data["keys"]
        controller.send_keys(text)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/scroll", methods=["POST"])
def scroll():
    try:
        data = request.get_json(force=True)
        x = int(data["x"])
        y = int(data["y"])
        direction = data["direction"]
        amount = int(data.get("amount", 3))
        controller.scroll(x, y, direction, amount)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/double_click", methods=["POST"])
def double_click():
    try:
        data = request.get_json(force=True)
        x = int(data["x"])
        y = int(data["y"])
        controller.double_click(x, y)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/screen_size", methods=["GET"])
def screen_size():
    try:
        size = controller.get_screen_size()
        return jsonify({"status": "ok", **size})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# Regex patterns for bracket command syntax
_PATTERNS = {
    "screenshot": re.compile(r"^\[ScreenShot\]$", re.IGNORECASE),
    "click": re.compile(r"^\[ClickMouse\((\d+),\s*(\d+)\)\]$", re.IGNORECASE),
    "move": re.compile(r"^\[MoveMouse\((\d+),\s*(\d+)\)\]$", re.IGNORECASE),
    "keys": re.compile(r"^\[SendKeys\((.+)\)\]$", re.IGNORECASE),
    "drag": re.compile(r"^\[DragMouse\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)\]$", re.IGNORECASE),
}


def _execute_command(cmd: str) -> dict:
    """Parse and execute a single bracket command. Returns a result dict."""
    cmd = cmd.strip()

    m = _PATTERNS["screenshot"].match(cmd)
    if m:
        image = controller.take_screenshot()
        return {"status": "ok", "image": image}

    m = _PATTERNS["click"].match(cmd)
    if m:
        controller.click_mouse(int(m.group(1)), int(m.group(2)))
        return {"status": "ok"}

    m = _PATTERNS["move"].match(cmd)
    if m:
        controller.move_mouse(int(m.group(1)), int(m.group(2)))
        return {"status": "ok"}

    m = _PATTERNS["keys"].match(cmd)
    if m:
        controller.send_keys(m.group(1))
        return {"status": "ok"}

    m = _PATTERNS["drag"].match(cmd)
    if m:
        controller.drag_mouse(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        return {"status": "ok"}

    return {"status": "error", "message": f"Unknown command: {cmd}"}


@app.route("/command", methods=["POST"])
def command():
    try:
        data = request.get_json(force=True)
        cmd = data.get("command", "").strip()
        result = _execute_command(cmd)
        code = 400 if result["status"] == "error" else 200
        return jsonify(result), code
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/actions", methods=["POST"])
def actions():
    """Execute a list of commands sequentially with a delay between each.

    Body: {"actions": ["[MoveMouse(100,200)]", "[ClickMouse(100,200)]", ...],
           "delay": 1.0}   // delay is optional, defaults to 1 second
    """
    try:
        data = request.get_json(force=True)
        action_list = data.get("actions", [])
        delay = float(data.get("delay", 1.0))

        if not action_list:
            return jsonify({"status": "error", "message": "actions list is empty"}), 400

        results = []
        for i, cmd in enumerate(action_list):
            result = _execute_command(cmd)
            results.append({"command": cmd, **result})
            # Don't sleep after the last action
            if i < len(action_list) - 1:
                time.sleep(delay)

        return jsonify({"status": "ok", "results": results})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


def run(host="0.0.0.0", port=7845):
    """Start the Flask server."""
    app.run(host=host, port=port, threaded=True)
