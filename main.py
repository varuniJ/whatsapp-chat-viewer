from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from db import collection
from datetime import datetime
import uuid
import os, json
from bson import ObjectId

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

# Optional: Enable CORS if API will be called from external frontend
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],  # Or specify your frontend URL(s) in production
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# Serve static frontend
app.mount("/static", StaticFiles(directory="static", html=True), name="static")
from fastapi.responses import RedirectResponse

@app.get("/")
def redirect_to_ui():
    return RedirectResponse(url="/static/index.html")

# --------------------------
# Load sample payloads from folder into MongoDB
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
                business_number = metadata.get("display_phone_number", None)

                if "messages" in value:
                    for msg in value["messages"]:
                        # Add "to" if missing in message payload
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
    data = list(collection.find(
        {"$or": [{"from": phone}, {"to": phone}]}
    ))
    if not data:
        raise HTTPException(status_code=404, detail="No conversation found for this number")
    data = serialize_doc(data)
    data.sort(key=lambda x: x.get("timestamp", ""))
    return {"phone": phone, "conversation": data}

from pydantic import BaseModel
class SendMessageRequest(BaseModel):
    from_number: str
    to_number: str
    message: str

@app.post("/send_message")
def send_message(req: SendMessageRequest):
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
    return {"status": "success", "message": serialize_doc(saved_msg)}
