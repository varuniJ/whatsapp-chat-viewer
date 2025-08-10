from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from db import collection
from datetime import datetime
import uuid
import os, json
from bson import ObjectId
from pydantic import BaseModel
from typing import List

# --------------------------
# Helper: serialize ObjectId
# --------------------------
def serialize_doc(doc):
    if isinstance(doc, list):
        return [serialize_doc(d) for d in doc]
    if isinstance(doc, dict):
        return {k: (str(v) if isinstance(v, ObjectId) else serialize_doc(v))
                for k, v in doc.items()}
    return doc

# --------------------------
# FastAPI app
# --------------------------
app = FastAPI()

# Optional CORS if needed
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# Serve static frontend
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def redirect_to_ui():
    return RedirectResponse(url="/static/index.html")

# --------------------------
# WebSocket Connection Manager
# --------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Not listening to client messages for now
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --------------------------
# Load sample payloads into MongoDB
# --------------------------
payload_folder = "sample_payloads"

def process_payloads():
    if collection.count_documents({}) > 0:
        print("📌 Data already exists in MongoDB, skipping load.")
        return

    print("📥 Loading payloads into MongoDB...")
    for filename in os.listdir(payload_folder):
        if filename.endswith(".json"):
            with open(os.path.join(payload_folder, filename), "r") as f:
                payload = json.load(f)

            entry_data = payload.get("metaData", {}).get("entry", [])
            if not entry_data:
                continue

            for change in entry_data[0].get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                business_number = metadata.get("display_phone_number")

                if "messages" in value:
                    for msg in value["messages"]:
                        if "to" not in msg and business_number:
                            msg["to"] = business_number
                        collection.update_one(
                            {"id": msg["id"]},
                            {"$set": msg},
                            upsert=True
                        )
                elif "statuses" in value:
                    for status in value["statuses"]:
                        collection.update_one(
                            {"id": status["id"]},
                            {"$set": {
                                "status": status["status"],
                                "timestamp": status["timestamp"]
                            }},
                            upsert=True
                        )
    print("✅ Payload loading complete.")

@app.on_event("startup")
def startup_event():
    process_payloads()

# --------------------------
# API endpoints
# --------------------------
@app.get("/phones")
def get_phones():
    phones = set()
    for doc in collection.find({}, {"from": 1, "to": 1, "_id": 0}):
        if "from" in doc:
            phones.add(doc["from"])
        if "to" in doc:
            phones.add(doc["to"])
    return {"phones": sorted(list(phones))}

@app.get("/conversations/{phone}")
def get_conversation(phone: str):
    data = list(collection.find({"$or": [{"from": phone}, {"to": phone}]}))
    if not data:
        raise HTTPException(status_code=404, detail="No conversation found for this number")
    data = serialize_doc(data)
    data.sort(key=lambda x: x.get("timestamp", ""))
    return {"phone": phone, "conversation": data}

class SendMessageRequest(BaseModel):
    from_number: str
    to_number: str
    message: str

@app.post("/send_message")
async def send_message(req: SendMessageRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    new_msg = {
        "id": str(uuid.uuid4()),
        "from": req.from_number,
        "to": req.to_number,
        "timestamp": datetime.utcnow().isoformat(),
        "text": {"body": req.message},
        "status": "sent"
    }
    inserted = collection.insert_one(new_msg)
    saved_msg = collection.find_one({"_id": inserted.inserted_id})
    serialized_msg = serialize_doc(saved_msg)

    # Broadcast to WebSocket clients
    await manager.broadcast({
        "event": "new_message",
        "message": serialized_msg
    })

    return {"status": "success", "message": serialized_msg}
