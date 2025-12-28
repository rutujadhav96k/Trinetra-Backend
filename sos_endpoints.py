# SOS Emergency Endpoints

from math import radians, cos, sin, asin, sqrt

def calculate_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate the great circle distance in kilometers between two points 
    on the earth (specified in decimal degrees)
    """
    # Convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lng1, lat1, lng2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371  # Radius of earth in kilometers
    return c * r


def find_nearby_officers(officer_id: str, lat: float, lng: float, radius_km: float = 2.0) -> List[str]:
    """Find officers within radius_km of the given location"""
    nearby = []
    for oid, officer in officers.items():
        if oid == officer_id or not officer.is_online:
            continue
        if officer.last_location:
            distance = calculate_distance(
                lat, lng,
                officer.last_location['lat'],
                officer.last_location['lng']
            )
            if distance <= radius_km:
                nearby.append(oid)
    return nearby


class SOSTrigger(BaseModel):
    lat: float
    lng: float
    officer_name: str
    badge_number: str | None = None
    emergency_type: str  # "high_emergency", "audio_message", "text_message"
    message_text: str | None = None
    audio_duration: float | None = None
    timestamp: str | None = None


class SOSCancel(BaseModel):
    reason: str = "cancelled_by_officer"


@app.post("/api/officers/{officer_id}/sos")
async def trigger_sos(
    officer_id: str,
    lat: float = Form(...),
    lng: float = Form(...),
    officer_name: str = Form(...),
    badge_number: str = Form(None),
    emergency_type: str = Form(...),
    message_text: str = Form(None),
    audio_duration: float = Form(None),
    audio: UploadFile = File(None),
) -> Dict[str, Any]:
    """
    Trigger SOS emergency alert
    Supports three types:
    - high_emergency: Immediate alert
    - audio_message: Voice message with location
    - text_message: Predefined text message
    """
    print(f"🚨 SOS TRIGGERED: Officer {officer_id} ({officer_name})")
    print(f"   Type: {emergency_type}")
    print(f"   Location: ({lat}, {lng})")
    
    now = datetime.utcnow()
    audio_url = None
    
    # Handle audio upload if present
    if audio and emergency_type == "audio_message":
        # Check if Cloudinary is configured
        use_cloudinary = all([
            os.getenv("CLOUDINARY_CLOUD_NAME"),
            os.getenv("CLOUDINARY_API_KEY"),
            os.getenv("CLOUDINARY_API_SECRET")
        ])
        
        if use_cloudinary:
            try:
                # Upload to Cloudinary
                result = cloudinary.uploader.upload(
                    audio.file,
                    folder="trinetra/sos_audio",
                    public_id=f"{officer_id}_{int(now.timestamp())}",
                    resource_type="auto"
                )
                audio_url = result['secure_url']
                print(f"✅ Audio uploaded to Cloudinary: {audio_url}")
            except Exception as e:
                print(f"❌ Cloudinary upload failed: {e}")
        else:
            # Save locally
            audio_path = f"static/uploads/sos_audio/{officer_id}_{int(now.timestamp())}.m4a"
            os.makedirs("static/uploads/sos_audio", exist_ok=True)
            with open(audio_path, "wb") as buffer:
                shutil.copyfileobj(audio.file, buffer)
            audio_url = f"/{audio_path}"
            print(f"✅ Audio saved locally: {audio_url}")
    
    # Update officer state
    async with officers_lock:
        if officer_id in officers:
            officer = officers[officer_id]
            officer.sos_active = True
            officer.sos_triggered_at = now
            officer.sos_type = emergency_type
            officer.sos_message = message_text
            officer.sos_audio_url = audio_url
        else:
            # Create new officer state
            officers[officer_id] = OfficerState(
                officer_id=officer_id,
                officer_name=officer_name,
                badge_number=badge_number,
                is_online=True,
                last_location={"lat": lat, "lng": lng, "timestamp": now.isoformat()},
                last_seen=now,
                sos_active=True,
                sos_triggered_at=now,
                sos_type=emergency_type,
                sos_message=message_text,
                sos_audio_url=audio_url,
            )
    
    # Find nearby officers
    nearby_officers = find_nearby_officers(officer_id, lat, lng, radius_km=2.0)
    print(f"📍 Found {len(nearby_officers)} nearby officers")
    
    # Save to MongoDB
    try:
        sos_event = {
            "officer_id": officer_id,
            "officer_name": officer_name,
            "badge_number": badge_number,
            "lat": lat,
            "lng": lng,
            "emergency_type": emergency_type,
            "message_text": message_text,
            "audio_url": audio_url,
            "audio_duration": audio_duration,
            "nearby_officers": nearby_officers,
            "status": "triggered",
            "triggered_at": now,
            "resolved_at": None,
        }
        
        # Create SOS events collection if it doesn't exist
        sos_collection = db.get_collection("sos_events")
        await sos_collection.insert_one(sos_event)
        print(f"✅ SOS event saved to MongoDB")
    except Exception as e:
        print(f"❌ MongoDB save error: {e}")
    
    # Broadcast to dashboard and nearby officers
    await broadcast({
        "type": "officer_sos_alert",
        "officer_id": officer_id,
        "officer_name": officer_name,
        "badge_number": badge_number,
        "lat": lat,
        "lng": lng,
        "sos_active": True,
        "emergency_type": emergency_type,
        "message_text": message_text,
        "audio_url": audio_url,
        "audio_duration": audio_duration,
        "triggered_at": now.isoformat(),
        "nearby_officers": nearby_officers,
    })
    
    print(f"✅ SOS alert broadcasted to {len(dashboard_clients)} clients")
    
    return {
        "ok": True,
        "message": "SOS triggered successfully",
        "nearby_officers": nearby_officers,
    }


@app.post("/api/officers/{officer_id}/sos/cancel")
async def cancel_sos(officer_id: str, payload: SOSCancel) -> Dict[str, Any]:
    """Cancel SOS alert (high emergency only)"""
    print(f"❌ SOS CANCELED: Officer {officer_id}")
    print(f"   Reason: {payload.reason}")
    
    async with officers_lock:
        if officer_id in officers:
            officer = officers[officer_id]
            officer.sos_active = False
            officer.sos_triggered_at = None
            officer.sos_type = None
            officer.sos_message = None
            officer.sos_audio_url = None
    
    # Update MongoDB
    try:
        sos_collection = db.get_collection("sos_events")
        await sos_collection.update_many(
            {
                "officer_id": officer_id,
                "status": "triggered"
            },
            {
                "$set": {
                    "status": "cancelled",
                    "cancelled_at": datetime.utcnow(),
                    "cancel_reason": payload.reason
                }
            }
        )
        print(f"✅ SOS cancellation saved to MongoDB")
    except Exception as e:
        print(f"❌ MongoDB update error: {e}")
    
    # Broadcast cancellation
    await broadcast({
        "type": "officer_sos_cancelled",
        "officer_id": officer_id,
        "reason": payload.reason,
    })
    
    return {"ok": True, "message": "SOS cancelled"}
