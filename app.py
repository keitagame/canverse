"""
canverse - リアルタイム協調ホワイトボード
FastAPI + WebSocket バックエンド
"""

import json
import asyncio
import random
import string
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

app = FastAPI(title="canverse")

# ユーザーごとの色パレット（見分けやすい色）
USER_COLORS = [
    "#E63946", "#2196F3", "#4CAF50", "#FF9800", "#9C27B0",
    "#00BCD4", "#FF5722", "#8BC34A", "#F44336", "#3F51B5",
    "#009688", "#FFC107", "#673AB7", "#CDDC39", "#E91E63",
]

def random_id(length=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

class ConnectionManager:
    def __init__(self):
        # room_id -> {user_id -> websocket}
        self.rooms: dict[str, dict[str, WebSocket]] = {}
        # room_id -> {user_id -> {color, name, cursor}}
        self.user_info: dict[str, dict[str, dict]] = {}
        # room_id -> [stroke_events] (最新2000件)
        self.stroke_history: dict[str, list] = {}

    def assign_color(self, room_id: str) -> str:
        used = {info["color"] for info in self.user_info.get(room_id, {}).values()}
        for c in USER_COLORS:
            if c not in used:
                return c
        return random.choice(USER_COLORS)

    async def connect(self, room_id: str, user_id: str, ws: WebSocket, name: str):
        await ws.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}
            self.user_info[room_id] = {}
            self.stroke_history[room_id] = []

        color = self.assign_color(room_id)
        self.rooms[room_id][user_id] = ws
        self.user_info[room_id][user_id] = {
            "color": color,
            "name": name,
            "cursor": None,
        }

        # 新規ユーザーに履歴を送信
        await ws.send_json({
            "type": "init",
            "userId": user_id,
            "color": color,
            "history": self.stroke_history[room_id],
            "users": self._get_users(room_id),
        })

        # 他ユーザーに参加通知
        await self.broadcast(room_id, {
            "type": "user_join",
            "userId": user_id,
            "name": name,
            "color": color,
            "users": self._get_users(room_id),
        }, exclude=user_id)

    def disconnect(self, room_id: str, user_id: str):
        if room_id in self.rooms:
            self.rooms[room_id].pop(user_id, None)
            self.user_info[room_id].pop(user_id, None)
            if not self.rooms[room_id]:
                # 部屋が空になったら履歴を保持（再入室できるように）
                pass

    async def broadcast(self, room_id: str, data: dict, exclude: Optional[str] = None):
        if room_id not in self.rooms:
            return
        dead = []
        for uid, ws in self.rooms[room_id].items():
            if uid == exclude:
                continue
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(uid)
        for uid in dead:
            self.disconnect(room_id, uid)

    async def send_to(self, room_id: str, user_id: str, data: dict):
        ws = self.rooms.get(room_id, {}).get(user_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(room_id, user_id)

    def _get_users(self, room_id: str) -> list:
        return [
            {"userId": uid, **info}
            for uid, info in self.user_info.get(room_id, {}).items()
        ]

    def add_history(self, room_id: str, event: dict):
        if room_id not in self.stroke_history:
            self.stroke_history[room_id] = []
        self.stroke_history[room_id].append(event)
        # 最新5000件のみ保持
        if len(self.stroke_history[room_id]) > 5000:
            self.stroke_history[room_id] = self.stroke_history[room_id][-5000:]

    def clear_history(self, room_id: str):
        if room_id in self.stroke_history:
            self.stroke_history[room_id] = []


manager = ConnectionManager()


@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(ws: WebSocket, room_id: str, user_id: str):
    name = ws.query_params.get("name", f"User-{user_id[:4]}")
    await manager.connect(room_id, user_id, ws, name)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "stroke":
                # 描画データを履歴に追加してブロードキャスト
                stroke_event = {
                    "type": "stroke",
                    "userId": user_id,
                    "color": manager.user_info[room_id][user_id]["color"],
                    "points": data.get("points", []),
                    "tool": data.get("tool", "pen"),
                    "size": data.get("size", 15),
                }
                manager.add_history(room_id, stroke_event)
                await manager.broadcast(room_id, stroke_event, exclude=user_id)

            elif msg_type == "cursor":
                # カーソル位置をブロードキャスト（履歴には残さない）
                cursor_event = {
                    "type": "cursor",
                    "userId": user_id,
                    "name": name,
                    "color": manager.user_info[room_id][user_id]["color"],
                    "x": data.get("x"),
                    "y": data.get("y"),
                }
                await manager.broadcast(room_id, cursor_event, exclude=user_id)

            elif msg_type == "clear":
                # 全員のキャンバスをクリア
                manager.clear_history(room_id)
                await manager.broadcast(room_id, {"type": "clear", "userId": user_id})

            elif msg_type == "undo":
                # 自分の最後のストロークを消す
                history = manager.stroke_history.get(room_id, [])
                # 逆順で自分のストロークを探す
                for i in range(len(history) - 1, -1, -1):
                    if history[i].get("userId") == user_id and history[i].get("type") == "stroke":
                        history.pop(i)
                        break
                # 全員に再描画を指示
                await manager.broadcast(room_id, {
                    "type": "redraw",
                    "history": manager.stroke_history.get(room_id, []),
                })
                await ws.send_json({
                    "type": "redraw",
                    "history": manager.stroke_history.get(room_id, []),
                })

    except WebSocketDisconnect:
        manager.disconnect(room_id, user_id)
        await manager.broadcast(room_id, {
            "type": "user_leave",
            "userId": user_id,
            "users": manager._get_users(room_id),
        })
    except Exception as e:
        print(f"Error: {e}")
        manager.disconnect(room_id, user_id)


@app.get("/")
async def root():
    return FileResponse("index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
