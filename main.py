from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Set
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env file

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
import os
import uuid
import shutil
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors

class LocationUpdate(BaseModel):
    lat: float
    lng: float
    speed: float | None = None
    alt: float | None = None
    heading: float | None = None
    nickname: str | None = None
    is_live: bool = True
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class StatusUpdate(BaseModel):
    is_live: bool
    nickname: str | None = None


@dataclass
class DroneState:
    drone_id: str
    nickname: str | None
    is_live: bool
    last_location: Dict[str, Any] | None
    last_seen: datetime


@dataclass
class OfficerState:
    officer_id: str
    officer_name: str
    badge_number: str | None
    is_online: bool
    last_location: Dict[str, Any] | None
    last_seen: datetime


# MongoDB Configuration
MONGO_DETAILS = os.getenv("MONGO_DETAILS", "mongodb://localhost:27017")

# Directory Setup
BASE_UPLOAD_DIR = "static/uploads"
os.makedirs("static/uploads/photos", exist_ok=True)
os.makedirs("static/uploads/docs", exist_ok=True)
os.makedirs("static/reports", exist_ok=True)

try:
    client = AsyncIOMotorClient(MONGO_DETAILS, serverSelectionTimeoutMS=5000)
    db = client.trinetra_db
    registration_collection = db.get_collection("registrations")
    officers_collection = db.get_collection("officers")
except Exception as e:
    print(f"CRITICAL: Failed to connect to MongoDB: {e}")

# Pydantic Models for Registration
class RegistrationSubmit(BaseModel):
    full_name: str
    mobile_number: str
    official_email: str
    dob: str
    badge_number: str
    rank: str
    station_name: str
    district: str
    state: str
    service_id: str
    biometric_enabled: bool = False

class OTPVerify(BaseModel):
    mobile_number: str
    otp: str

class DeviceBind(BaseModel):
    officer_id: str
    otp: str
    device_id: str

class OfficerLogin(BaseModel):
    officer_id: str
    device_id: str


app = FastAPI(title="Drone Live Location", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",  # Local frontend development
        "http://127.0.0.1:5500",
        "https://*.netlify.app",  # Netlify deployments (wildcard)
        os.getenv("FRONTEND_URL", ""),  # Production frontend URL from env
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

drones: Dict[str, DroneState] = {}
officers: Dict[str, OfficerState] = {}
dashboard_clients: Set[WebSocket] = set()
drones_lock = asyncio.Lock()
officers_lock = asyncio.Lock()
clients_lock = asyncio.Lock()

STALE_AFTER = timedelta(seconds=90)


# --- NEW: LOAD DATA ON STARTUP ---
@app.on_event("startup")
async def load_officers_from_db():
    """
    Loads officers who have a known last location from the database on server restart.
    This ensures data appears immediately on the frontend without waiting for new updates.
    """
    print("Loading officer states from database...")
    count = 0
    try:
        # Find officers who have a 'last_location' field
        async for doc in officers_collection.find({"last_location": {"$ne": None}}):
            officer_id = doc["officer_id"]
            
            # Determine timestamp (handle ISO string or datetime object)
            last_seen_val = datetime.utcnow()
            if "last_seen" in doc:
                if isinstance(doc["last_seen"], datetime):
                    last_seen_val = doc["last_seen"]
                elif isinstance(doc["last_seen"], str):
                    try:
                        last_seen_val = datetime.fromisoformat(doc["last_seen"])
                    except:
                        pass

            # Populate memory
            officers[officer_id] = OfficerState(
                officer_id=officer_id,
                officer_name=doc.get("full_name", "Unknown Officer"),
                badge_number=doc.get("badge_number"),
                is_online=False, # Mark as offline initially until they connect
                last_location=doc.get("last_location"),
                last_seen=last_seen_val
            )
            count += 1
        print(f"Loaded {count} officers from DB.")
    except Exception as e:
        print(f"Error loading officers on startup: {e}")


async def prune_stale_drones() -> None:
    async with drones_lock:
        now = datetime.utcnow()
        stale_ids = [
            drone_id
            for drone_id, state in drones.items()
            if state.is_live and now - state.last_seen > STALE_AFTER
        ]
        for drone_id in stale_ids:
            drones[drone_id].is_live = False
            await broadcast(
                {
                    "type": "status",
                    "drone_id": drone_id,
                    "is_live": False,
                    "nickname": drones[drone_id].nickname,
                }
            )


async def prune_stale_officers() -> None:
    async with officers_lock:
        now = datetime.utcnow()
        stale_ids = [
            officer_id
            for officer_id, state in officers.items()
            if state.is_online and now - state.last_seen > STALE_AFTER
        ]
        for officer_id in stale_ids:
            officers[officer_id].is_online = False
            await broadcast(
                {
                    "type": "officer_status",
                    "officer_id": officer_id,
                    "is_online": False,
                    "officer_name": officers[officer_id].officer_name,
                }
            )


async def broadcast(message: Dict[str, Any]) -> None:
    await prune_stale_drones()
    await prune_stale_officers()
    async with clients_lock:
        if not dashboard_clients:
            return
        closed: List[WebSocket] = []
        for ws in dashboard_clients:
            try:
                await ws.send_json(message)
            except Exception:
                closed.append(ws)
        for ws in closed:
            dashboard_clients.discard(ws)


def _serialize_drone(state: DroneState) -> Dict[str, Any]:
    return {
        "drone_id": state.drone_id,
        "nickname": state.nickname,
        "is_live": state.is_live,
        "last_location": state.last_location,
        "last_seen": state.last_seen.isoformat(),
    }


@app.get("/api/drones")
async def list_drones() -> Dict[str, Any]:
    async with drones_lock:
        return {"drones": [_serialize_drone(d) for d in drones.values()]}


@app.post("/api/drones/{drone_id}/status")
async def update_status(drone_id: str, payload: StatusUpdate) -> Dict[str, Any]:
    async with drones_lock:
        current = drones.get(
            drone_id,
            DroneState(
                drone_id=drone_id,
                nickname=payload.nickname,
                is_live=payload.is_live,
                last_location=None,
                last_seen=datetime.utcnow(),
            ),
        )
        current.is_live = payload.is_live
        if payload.nickname:
            current.nickname = payload.nickname
        current.last_seen = datetime.utcnow()
        drones[drone_id] = current

    await broadcast(
        {
            "type": "status",
            "drone_id": drone_id,
            "is_live": payload.is_live,
            "nickname": payload.nickname or current.nickname,
        }
    )
    if payload.is_live:
        print(f"Drone '{drone_id}' is connected")
    else:
        print(f"Drone '{drone_id}' disconnected")
        
    return {"ok": True}


@app.post("/api/drones/{drone_id}/location")
async def ingest_location(drone_id: str, payload: LocationUpdate) -> Dict[str, Any]:
    async with drones_lock:
        state = drones.get(
            drone_id,
            DroneState(
                drone_id=drone_id,
                nickname=payload.nickname,
                is_live=payload.is_live,
                last_location=None,
                last_seen=datetime.utcnow(),
            ),
        )
        state.nickname = payload.nickname or state.nickname
        state.is_live = payload.is_live
        state.last_seen = payload.timestamp
        state.last_location = {
            "lat": payload.lat,
            "lng": payload.lng,
            "speed": payload.speed,
            "alt": payload.alt,
            "heading": payload.heading,
            "timestamp": payload.timestamp.isoformat(),
        }
        drones[drone_id] = state

    await broadcast(
        {
            "type": "location_update",
            "drone_id": drone_id,
            "nickname": state.nickname,
            "is_live": state.is_live,
            "lat": payload.lat,
            "lng": payload.lng,
            "speed": payload.speed,
            "alt": payload.alt,
            "heading": payload.heading,
            "timestamp": payload.timestamp.isoformat(),
        }

    )
    print(f"Received location from Drone '{drone_id}': Lat={payload.lat}, Lng={payload.lng}, Speed={payload.speed}")
    return {"ok": True}


# --- OFFICER LOCATION TRACKING ENDPOINTS ---

class OfficerLocationUpdate(BaseModel):
    lat: float
    lng: float
    officer_name: str
    badge_number: str | None = None
    accuracy: float | None = None
    timestamp: str | None = None  # Accept string timestamp from Flutter


class OfficerStatusUpdate(BaseModel):
    is_online: bool
    officer_name: str
    badge_number: str | None = None


def _serialize_officer(state: OfficerState) -> Dict[str, Any]:
    return {
        "officer_id": state.officer_id,
        "officer_name": state.officer_name,
        "badge_number": state.badge_number,
        "is_online": state.is_online,
        "last_location": state.last_location,
        "last_seen": state.last_seen.isoformat(),
    }


@app.get("/api/officers")
async def list_officers() -> Dict[str, Any]:
    async with officers_lock:
        return {"officers": [_serialize_officer(o) for o in officers.values()]}


@app.post("/api/officers/{officer_id}/status")
async def update_officer_status(officer_id: str, payload: OfficerStatusUpdate) -> Dict[str, Any]:
    async with officers_lock:
        current = officers.get(
            officer_id,
            OfficerState(
                officer_id=officer_id,
                officer_name=payload.officer_name,
                badge_number=payload.badge_number,
                is_online=payload.is_online,
                last_location=None,
                last_seen=datetime.utcnow(),
            ),
        )
        current.is_online = payload.is_online
        if payload.officer_name:
            current.officer_name = payload.officer_name
        if payload.badge_number:
            current.badge_number = payload.badge_number
        current.last_seen = datetime.utcnow()
        officers[officer_id] = current

    await broadcast(
        {
            "type": "officer_status",
            "officer_id": officer_id,
            "is_online": payload.is_online,
            "officer_name": payload.officer_name,
            "badge_number": payload.badge_number,
        }
    )
    if payload.is_online:
        print(f"Officer '{officer_id}' ({payload.officer_name}) is online")
    else:
        print(f"Officer '{officer_id}' ({payload.officer_name}) went offline")
        
    return {"ok": True}


# --- UPDATED INGEST LOCATION ---
@app.post("/api/officers/{officer_id}/location")
async def ingest_officer_location(officer_id: str, payload: OfficerLocationUpdate) -> Dict[str, Any]:
    print(f"📍 RECEIVED OFFICER LOCATION: {officer_id}")
    print(f"   Lat: {payload.lat}, Lng: {payload.lng}")
    print(f"   Name: {payload.officer_name}, Accuracy: {payload.accuracy}m")
    
    # 1. FILTER: High Accuracy Check (Discard inaccurate GPS data > 50m)
    if payload.accuracy and payload.accuracy > 50.0:
        print(f"⚠️ Skipping update for {officer_id}: Poor accuracy ({payload.accuracy}m)")
        return {"ok": False, "reason": "Poor accuracy"}

    now = datetime.utcnow()
    
    # 2. PERSISTENCE: Save location to MongoDB immediately
    location_data = {
        "lat": payload.lat,
        "lng": payload.lng,
        "accuracy": payload.accuracy,
        "timestamp": now.isoformat(),
    }
    
    await officers_collection.update_one(
        {"officer_id": officer_id},
        {"$set": {"last_location": location_data, "last_seen": now}}
    )
    print(f"✅ Saved to MongoDB")
    
    # 3. MEMORY UPDATE: Update in-memory state
    async with officers_lock:
        state = officers.get(
            officer_id,
            OfficerState(
                officer_id=officer_id,
                officer_name=payload.officer_name,
                badge_number=payload.badge_number,
                is_online=True,
                last_location=None,
                last_seen=now,
            ),
        )
        state.officer_name = payload.officer_name or state.officer_name
        state.badge_number = payload.badge_number or state.badge_number
        state.is_online = True
        state.last_seen = now
        state.last_location = location_data
        officers[officer_id] = state
    
    print(f"✅ Updated in-memory state")
    print(f"📡 Broadcasting to {len(dashboard_clients)} dashboard clients...")

    # 4. BROADCAST: Send to dashboard
    await broadcast(
        {
            "type": "officer_location_update",
            "officer_id": officer_id,
            "officer_name": state.officer_name,
            "badge_number": state.badge_number,
            "is_online": state.is_online,
            "lat": payload.lat,
            "lng": payload.lng,
            "accuracy": payload.accuracy,
            "timestamp": now.isoformat(),
        }
    )
    print(f"✅ Broadcast complete for officer {officer_id}")
    return {"ok": True}


@app.websocket("/ws/locations")
async def websocket_locations(websocket: WebSocket) -> None:
    await websocket.accept()
    async with clients_lock:
        dashboard_clients.add(websocket)

    # Send snapshot of both drones and officers
    async with drones_lock:
        drones_snapshot = [_serialize_drone(d) for d in drones.values()]
    async with officers_lock:
        officers_snapshot = [_serialize_officer(o) for o in officers.values()]
    
    await websocket.send_json({
        "type": "snapshot", 
        "drones": drones_snapshot,
        "officers": officers_snapshot
    })

    try:
        while True:
            # Keep connection alive; ignore inbound messages for now.
            await websocket.receive_text()
    except WebSocketDisconnect:
        async with clients_lock:
            dashboard_clients.discard(websocket)



# Dashboard is now served separately on Netlify
# Keeping /static mount for uploaded files and reports
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- PROFESSIONAL PDF GENERATOR ---
def generate_registration_pdf(data: dict, filename: str):
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4
    
    # --- 1. Header Section ---
    # Draw Header Background
    c.setFillColor(colors.darkblue)
    c.rect(0, height - 100, width, 100, fill=1, stroke=0)
    
    # Header Text
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, height - 50, "MAHARASHTRA POLICE")
    
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - 70, "OFFICER REGISTRATION DOSSIER")
    
    c.setFont("Helvetica-Oblique", 10)
    c.drawCentredString(width / 2, height - 85, f"Ref ID: {data.get('request_id', 'N/A')}")

    # --- 2. Officer Photo (Passport Style) ---
    # Position: Top Right, below header
    photo_x = width - 180
    photo_y = height - 260
    photo_w = 120
    photo_h = 140
    
    # Draw Border for Photo
    c.setStrokeColor(colors.black)
    c.setLineWidth(1)
    c.rect(photo_x, photo_y, photo_w, photo_h, stroke=1, fill=0)
    
    # Try to draw the actual image
    try:
        if os.path.exists(data['photo_path']):
            c.drawImage(data['photo_path'], photo_x + 2, photo_y + 2, width=photo_w-4, height=photo_h-4, preserveAspectRatio=True)
        else:
            c.setFillColor(colors.gray)
            c.drawString(photo_x + 10, photo_y + 70, "No Photo")
    except Exception as e:
        print(f"PDF Image Error: {e}")

    # --- 3. Officer Details (Left Side) ---
    c.setFillColor(colors.black)
    y_pos = height - 140
    x_pos = 50
    line_height = 25
    
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x_pos, y_pos, "PERSONAL & SERVICE DETAILS")
    y_pos -= 30
    
    # Define fields to show
    fields = [
        ("Full Name", data.get("full_name")),
        ("Rank", data.get("rank")),
        ("Badge Number", data.get("badge_number")),
        ("Service ID", data.get("service_id")),
        ("Station Name", data.get("station_name")),
        ("District", data.get("district")),
        ("State", data.get("state")),
        ("Mobile Number", data.get("mobile_number")),
        ("Official Email", data.get("official_email")),
        ("Date of Birth", data.get("dob")),
        ("Biometric", "Enabled" if data.get("biometric_enabled") else "Disabled"),
    ]

    c.setFont("Helvetica", 11)
    for label, value in fields:
        # Avoid writing over the photo area
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x_pos, y_pos, f"{label}:")
        c.setFont("Helvetica", 11)
        c.drawString(x_pos + 120, y_pos, str(value))
        
        # Draw underline
        c.setStrokeColor(colors.lightgrey)
        c.line(x_pos, y_pos - 5, width - 200, y_pos - 5)
        
        y_pos -= line_height

    # --- 4. ID Proof Attachment ---
    y_pos -= 30
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x_pos, y_pos, "ATTACHED IDENTITY PROOF")
    y_pos -= 20
    
    try:
        if os.path.exists(data['id_card_path']):
            # Draw ID card image scaled down to fit bottom
            # Calculate aspect ratio to fit in remaining space
            c.drawImage(data['id_card_path'], x_pos, 100, width=400, height=200, preserveAspectRatio=True, anchor='sw')
    except Exception:
        c.drawString(x_pos, y_pos - 20, "[ID Image Not Available]")

    # --- 5. Footer ---
    c.setStrokeColor(colors.black)
    c.line(50, 50, width - 50, 50)
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(50, 35, f"Generated via TRINETRA Command System | {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    c.drawRightString(width - 50, 35, "CONFIDENTIAL")

    c.save()

# Registration Endpoints
@app.post("/api/register")
async def register_officer(
    full_name: str = Form(...),
    mobile_number: str = Form(...),
    official_email: str = Form(...),
    dob: str = Form(...),
    badge_number: str = Form(...),
    rank: str = Form(...),
    station_name: str = Form(...),
    district: str = Form(...),
    state: str = Form(...),
    service_id: str = Form(...),
    biometric_enabled: bool = Form(False),
    photo: UploadFile = File(...),
    id_card: UploadFile = File(...)
):
    request_id = str(uuid.uuid4())
    
    # Save files
    photo_path = f"static/uploads/photos/{request_id}_{photo.filename}"
    id_card_path = f"static/uploads/docs/{request_id}_{id_card.filename}"
    
    with open(photo_path, "wb") as buffer:
        shutil.copyfileobj(photo.file, buffer)
    with open(id_card_path, "wb") as buffer:
        shutil.copyfileobj(id_card.file, buffer)
    
    registration_data = {
        "request_id": request_id,
        "full_name": full_name,
        "mobile_number": mobile_number,
        "official_email": official_email,
        "dob": dob,
        "badge_number": badge_number,
        "rank": rank,
        "station_name": station_name,
        "district": district,
        "state": state,
        "service_id": service_id,
        "biometric_enabled": biometric_enabled,
        "photo_path": photo_path,
        "id_card_path": id_card_path,
        "status": "Pending",
        "created_at": datetime.utcnow().isoformat()
    }

    # Generate PDF
    pdf_path = f"static/reports/{request_id}_report.pdf"
    generate_registration_pdf(registration_data, pdf_path)
    registration_data["pdf_path"] = pdf_path

    await registration_collection.insert_one(registration_data)
    return {"status": "success", "request_id": request_id}

@app.get("/api/admin/requests")
async def get_registration_requests():
    requests = []
    async for req in registration_collection.find({"status": "Pending"}):
        req["_id"] = str(req["_id"])
        requests.append(req)
    return {"requests": requests}

@app.post("/api/admin/approve/{request_id}")
async def approve_registration(request_id: str):
    req = await registration_collection.find_one({"request_id": request_id})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    
    # Generate officer ID (No password needed for passwordless auth)
    officer_id = f"POL-{uuid.uuid4().hex[:6].upper()}"

    officer_data = {
        "officer_id": officer_id,
        "full_name": req["full_name"],
        "mobile_number": req["mobile_number"],
        "badge_number": req["badge_number"],
        "device_id": None, # Will be set during device binding
        "status": "Active",
        "created_at": datetime.utcnow().isoformat()
    }

    await officers_collection.insert_one(officer_data)
    await registration_collection.update_one({"request_id": request_id}, {"$set": {"status": "Approved"}})

    # In a real app, send registration success notification here
    print(f"APPROVE: Officer ID assigned: {officer_id} for {req['official_email']}")

    return {"status": "approved", "officer_id": officer_id}

# OTP Storage with Expiry
@dataclass
class OTPData:
    code: str
    expires_at: datetime

temp_otps: Dict[str, OTPData] = {}

class DeviceReset(BaseModel):
    mobile_number: str
    otp: str
    badge_number: str

@app.post("/api/send-otp")
async def send_otp(mobile_number: str):
    # Generate Real 6-digit OTP
    import random
    otp_code = str(random.randint(100000, 999999))
    expires_at = datetime.utcnow() + timedelta(minutes=5)
    
    temp_otps[mobile_number] = OTPData(code=otp_code, expires_at=expires_at)
    
    # --- REAL SMS INTEGRATION ---
    # await send_sms_msg91(mobile_number, otp_code) 
    await send_sms_twilio(mobile_number, otp_code)
    
    print(f"SECURITY: OTP sent to {mobile_number} via Twilio")
    
    return {"status": "success", "message": "OTP sent via SMS"}

# --- SMS Gateway Helpers ---

async def send_sms_msg91(mobile: str, otp: str):
    """
    Best for Indian DLT Compliance.
    Requires: MSG91_AUTH_KEY, TEMPLATE_ID (DLT Approved)
    """
    import httpx
    url = "https://control.msg91.com/api/v5/otp"
    payload = {
        "template_id": "YOUR_DLT_TEPLATE_ID",
        "mobile": "91" + mobile,
        "authkey": "YOUR_MSG91_AUTH_KEY",
        "otp": otp
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"SMS Error: {e}")

async def send_sms_twilio(mobile: str, otp: str):
    try:
        from twilio.rest import Client
        import os
        
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_FROM_NUMBER") # e.g., +1234567890

        if not account_sid or not auth_token or not from_number:
            print("Twilio Config Missing: Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_NUMBER")
            return

        client = Client(account_sid, auth_token)
        
        # Ensure mobile number has country code (Assuming India +91 for now if missing)
        to_number = mobile if mobile.startswith("+") else f"+91{mobile}"

        message = client.messages.create(
            body=f"Your Trinetra Verification Code is: {otp}",
            from_=from_number,
            to=to_number
        )
        print(f"Twilio SMS Sent: SID {message.sid}")
        
    except Exception as e:
        print(f"Twilio Error: {e}")

@app.post("/api/verify-otp")
async def verify_otp(payload: OTPVerify):
    otp_data = temp_otps.get(payload.mobile_number)
    
    if not otp_data:
         raise HTTPException(status_code=400, detail="OTP not requested or expired")
    
    if datetime.utcnow() > otp_data.expires_at:
        del temp_otps[payload.mobile_number]
        raise HTTPException(status_code=400, detail="OTP expired")
        
    if otp_data.code != payload.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    # OTP is valid. Check if officer exists.
    officer = await officers_collection.find_one({"mobile_number": payload.mobile_number})
    
    if not officer:
        return {"status": "verified", "registered": False}
        
    return {"status": "verified", "registered": True, "officer_id": officer["officer_id"]}

@app.post("/api/officer/bind-device")
async def bind_device(payload: DeviceBind):
    # 1. Validate OTP again to ensure the request is fresh and authenticated
    officer = await officers_collection.find_one({"officer_id": payload.officer_id})
    if not officer:
         raise HTTPException(status_code=404, detail="Officer not found")
    
    otp_data = temp_otps.get(officer["mobile_number"])
    if not otp_data or otp_data.code != payload.otp:
         raise HTTPException(status_code=400, detail="Invalid or expired OTP for binding")

    # 2. Enforce "One Device Per Officer"
    current_device = officer.get("device_id")
    if current_device and current_device != payload.device_id:
        if current_device != payload.device_id:
            raise HTTPException(
                status_code=403, 
                detail="Account already bound to another device. Use 'Lost Device' recovery to reset."
            )

    # 3. Enforce Global "One Officer Per Device"
    existing_binding = await officers_collection.find_one({"device_id": payload.device_id})
    if existing_binding and existing_binding["officer_id"] != payload.officer_id:
        raise HTTPException(status_code=403, detail="This device is already registered to another officer.")

    # 4. Bind
    await officers_collection.update_one(
        {"officer_id": payload.officer_id}, 
        {"$set": {"device_id": payload.device_id, "last_login": datetime.utcnow()}}
    )
    
    # Clear OTP after successful binding
    if officer["mobile_number"] in temp_otps:
        del temp_otps[officer["mobile_number"]]
    
    return {
        "status": "success", 
        "officer": {
            "officer_id": officer["officer_id"], 
            "full_name": officer["full_name"]
        }
    }

@app.post("/api/officer/reset-device")
async def reset_device(payload: DeviceReset):
    """
    Allow an officer to unlink their old device if lost/broken.
    """
    # 1. Verify OTP
    otp_data = temp_otps.get(payload.mobile_number)
    if not otp_data or otp_data.code != payload.otp:
         raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # 2. Verify Officer and Badge Number
    officer = await officers_collection.find_one({
        "mobile_number": payload.mobile_number,
        "badge_number": payload.badge_number # strict check
    })
    
    if not officer:
        raise HTTPException(status_code=404, detail="Officer details mismatch")
        
    # 3. Reset Device ID
    await officers_collection.update_one(
        {"_id": officer["_id"]},
        {"$set": {"device_id": None}}
    )
    
    print(f"SECURITY: Device reset for Officer {officer['full_name']} (ID: {officer['officer_id']})")
    
    return {"status": "success", "message": "Device binding cleared. You can now login on a new device."}

@app.post("/api/check-device")
async def check_device(payload: dict):
    """
    Check if a device is already registered in the system.
    Used on app startup to detect reinstalls on same device.
    """
    device_id = payload.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="Device ID required")
    
    # Find officer with this device
    officer = await officers_collection.find_one({"device_id": device_id})
    
    if officer:
        return {
            "registered": True,
            "mobile_number": officer["mobile_number"],
            "officer_id": officer["officer_id"]
        }
    
    return {"registered": False}

@app.post("/api/officer/validate-device")
async def validate_device(login: OfficerLogin):
    officer = await officers_collection.find_one({"officer_id": login.officer_id})
    if not officer:
        raise HTTPException(status_code=401, detail="Invalid Officer ID")
    
    # If device_id is None, they must re-bind (likely reset happened)
    if not officer.get("device_id"):
         raise HTTPException(status_code=403, detail="Device not bound. Please login again.")
    
    if officer["device_id"] != login.device_id:
        raise HTTPException(status_code=403, detail="Unauthorized device. Please use your registered device.")
    
    return {"status": "authorized", "full_name": officer["full_name"]}


@app.get("/api/officer/{officer_id}/details")
async def get_officer_details(officer_id: str):
    """
    Get complete officer details including registration data and photo.
    This combines data from officers collection and registration collection.
    """
    print(f"DEBUG: Fetching details for officer_id: {officer_id}")
    
    # Get basic officer data
    officer = await officers_collection.find_one({"officer_id": officer_id})
    if not officer:
        print(f"DEBUG: Officer {officer_id} not found in officers collection")
        raise HTTPException(status_code=404, detail="Officer not found")
    
    print(f"DEBUG: Found officer: {officer.get('full_name')}, mobile: {officer.get('mobile_number')}")
    
    # Get full registration data (includes photo, rank, station, etc.)
    registration = await registration_collection.find_one({
        "mobile_number": officer["mobile_number"],
        "status": "Approved"
    })
    
    if registration:
        print(f"DEBUG: Found registration data with photo: {registration.get('photo_path')}")
    else:
        print(f"DEBUG: No approved registration found for mobile: {officer.get('mobile_number')}")
    
    # Combine data
    officer_details = {
        "officer_id": officer["officer_id"],
        "full_name": officer["full_name"],
        "mobile_number": officer["mobile_number"],
        "badge_number": officer["badge_number"],
        "status": officer.get("status", "Active")
    }
    
    # Add registration details if available
    if registration:
        officer_details.update({
            "rank": registration.get("rank"),
            "station_name": registration.get("station_name"),
            "district": registration.get("district"),
            "state": registration.get("state"),
            "official_email": registration.get("official_email"),
            "photo_path": registration.get("photo_path"),
            "service_id": registration.get("service_id"),
            "dob": registration.get("dob")
        })
    
    print(f"DEBUG: Returning officer details: {officer_details}")
    return officer_details


def get_app() -> FastAPI:
    return app



video_viewers: Set[WebSocket] = set()
# Store the drone connection (The Sender)
drone_camera_socket: WebSocket | None = None

@app.websocket("/ws/video/feed")
async def websocket_video_feed(websocket: WebSocket):
    global drone_camera_socket
    await websocket.accept()
    video_viewers.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        video_viewers.discard(websocket)

@app.websocket("/ws/video/upload")
async def websocket_video_upload(websocket: WebSocket):
    # This endpoint is for the DRONE to upload frames
    global drone_camera_socket
    await websocket.accept()
    drone_camera_socket = websocket
    print("Drone Camera Connected!")
    
    try:
        while True:
            # Receive raw bytes (the image frame)
            data = await websocket.receive_bytes()
            
            # Broadcast to all viewers IMMEDIATELY
            for viewer in list(video_viewers):
                try:
                    await viewer.send_bytes(data)
                except Exception:
                    video_viewers.discard(viewer)
    except WebSocketDisconnect:
        print("Drone Camera Disconnected")
        drone_camera_socket = None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)