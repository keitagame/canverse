#!/usr/bin/env python3
"""
Canverse - Real-time collaborative drawing platform
Single-server: HTTP + WebSocket on port 8080
"""

import asyncio
import json
import logging
import uuid
from typing import Dict
import http
import os
import websockets
from websockets.server import WebSocketServerProtocol

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────
clients: Dict[WebSocketServerProtocol, dict] = {}
stroke_history: list = []
MAX_HISTORY = 20000

USER_COLORS = [
    "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
    "#9b59b6", "#1abc9c", "#e67e22", "#34495e",
    "#e91e63", "#00bcd4", "#8bc34a", "#ff5722",
    "#607d8b", "#795548", "#ff9800", "#009688",
]
color_index = 0

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

def get_html() -> bytes:
    with open(HTML_PATH, "rb") as f:
        return f.read()

# ── HTTP handler ─────────────────────────────────
async def http_handler(path, request_headers):
    if request_headers.get("Upgrade", "").lower() == "websocket":
        return None  # let WebSocket through
    body = get_html()
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-cache"),
    ]
    return http.HTTPStatus.OK, headers, body

# ── Broadcast helpers ─────────────────────────────
async def broadcast(message: dict, exclude=None):
    if not clients:
        return
    data = json.dumps(message)
    targets = [ws for ws in clients if ws != exclude]
    if targets:
        await asyncio.gather(*[ws.send(data) for ws in targets], return_exceptions=True)

async def broadcast_all(message: dict):
    if not clients:
        return
    data = json.dumps(message)
    await asyncio.gather(*[ws.send(data) for ws in clients], return_exceptions=True)

# ── WebSocket handler ────────────────────────────
async def ws_handler(websocket: WebSocketServerProtocol):
    global color_index

    user_id    = str(uuid.uuid4())[:8]
    user_color = USER_COLORS[color_index % len(USER_COLORS)]
    color_index += 1

    clients[websocket] = {"id": user_id, "color": user_color, "x": 0, "y": 0}
    logger.info(f"[+] {user_id}  total={len(clients)}")

    try:
        await websocket.send(json.dumps({
            "type":      "init",
            "userId":    user_id,
            "userColor": user_color,
            "history":   stroke_history[-3000:],
            "users": [
                {"id": v["id"], "color": v["color"], "x": v["x"], "y": v["y"]}
                for v in clients.values()
            ],
        }))
        await broadcast({"type": "user_joined", "userId": user_id, "color": user_color}, exclude=websocket)
        await broadcast_all({"type": "user_count", "count": len(clients)})

        async for raw in websocket:
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "draw":
                    color = str(msg.get("color", "#000000"))
                    if color == "__eraser__":
                        color = "#ffffff"
                    size = max(1, min(60, int(msg.get("size", 4))))
                    event = {
                        "type": "draw", "userId": user_id,
                        "color": color, "size": size,
                        "x0": float(msg.get("x0", 0)), "y0": float(msg.get("y0", 0)),
                        "x1": float(msg.get("x1", 0)), "y1": float(msg.get("y1", 0)),
                    }
                    stroke_history.append(event)
                    if len(stroke_history) > MAX_HISTORY:
                        del stroke_history[:MAX_HISTORY // 10]
                    await broadcast(event, exclude=websocket)

                elif msg_type == "cursor":
                    clients[websocket]["x"] = float(msg.get("x", 0))
                    clients[websocket]["y"] = float(msg.get("y", 0))
                    await broadcast({
                        "type": "cursor", "userId": user_id, "color": user_color,
                        "x": clients[websocket]["x"], "y": clients[websocket]["y"],
                    }, exclude=websocket)

                # "clear" is intentionally NOT handled — users cannot delete drawings

            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(f"Bad message from {user_id}: {e}")

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        del clients[websocket]
        logger.info(f"[-] {user_id}  total={len(clients)}")
        await broadcast({"type": "user_left", "userId": user_id})
        await broadcast_all({"type": "user_count", "count": len(clients)})

# ── Main ─────────────────────────────────────────
async def main():
    host, port = "0.0.0.0", 8080
    logger.info(f"Canverse  →  http://localhost:{port}")
    async with websockets.serve(
        ws_handler, host, port,
        process_request=http_handler,
        max_size=2 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=40,
    ):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
