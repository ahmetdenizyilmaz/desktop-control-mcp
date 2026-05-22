# Desktop Control API Documentation

Local HTTP API running on `http://localhost:7845`

## Endpoints

### GET /health

Check if the server is running.

```bash
curl http://localhost:7845/health
```

Response:
```json
{"status": "ok"}
```

---

### POST /screenshot

Take a screenshot of the entire screen.

```bash
curl -X POST http://localhost:7845/screenshot
```

Response:
```json
{"status": "ok", "image": "<base64 encoded PNG>"}
```

---

### POST /move

Move the mouse cursor to a position.

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"x": 500, "y": 300}' \
  http://localhost:7845/move
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x | int | yes | X coordinate |
| y | int | yes | Y coordinate |

---

### POST /click

Click the mouse at a position.

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"x": 500, "y": 300, "button": "left"}' \
  http://localhost:7845/click
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x | int | yes | X coordinate |
| y | int | yes | Y coordinate |
| button | string | no | `"left"` (default), `"right"`, or `"middle"` |

---

### POST /keys

Type text or send a hotkey combo.

```bash
# Type text
curl -X POST -H "Content-Type: application/json" \
  -d '{"keys": "hello world"}' \
  http://localhost:7845/keys

# Hotkey combo
curl -X POST -H "Content-Type: application/json" \
  -d '{"keys": "ctrl+c"}' \
  http://localhost:7845/keys
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| keys | string | yes | Text to type, or a hotkey combo like `"ctrl+c"`, `"alt+tab"` |

---

### POST /command

Execute a single action using bracket syntax.

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"command": "[ClickMouse(500,300)]"}' \
  http://localhost:7845/command
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| command | string | yes | A bracket command (see syntax below) |

**Bracket command syntax:**

| Command | Description |
|---------|-------------|
| `[ScreenShot]` | Take a screenshot |
| `[MoveMouse(x,y)]` | Move cursor to (x, y) |
| `[ClickMouse(x,y)]` | Left-click at (x, y) |
| `[SendKeys(text)]` | Type text or hotkey |

---

### POST /actions

Execute multiple commands sequentially with a delay between each.

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{
    "actions": [
      "[MoveMouse(300,300)]",
      "[ClickMouse(500,400)]",
      "[SendKeys(hello)]",
      "[ScreenShot]"
    ],
    "delay": 1.0
  }' \
  http://localhost:7845/actions
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| actions | list | yes | List of bracket commands to execute in order |
| delay | float | no | Seconds to wait between each action (default: `1.0`) |

**Response:**

```json
{
  "status": "ok",
  "results": [
    {"command": "[MoveMouse(300,300)]", "status": "ok"},
    {"command": "[ClickMouse(500,400)]", "status": "ok"},
    {"command": "[SendKeys(hello)]", "status": "ok"},
    {"command": "[ScreenShot]", "status": "ok", "image": "<base64 PNG>"}
  ]
}
```

Each action returns its own result. If an action fails, its entry will have `"status": "error"` with a `"message"` field, but the remaining actions will still execute.

---

## Error Responses

All endpoints return errors in this format:

```json
{"status": "error", "message": "description of what went wrong"}
```

## Running the App

```bash
pip install -r requirements.txt
python app.py
```

The app starts a system tray icon and the HTTP server on port 7845. Use the tray menu to stop/start the server or quit.
