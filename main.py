from __future__ import annotations

import os
import sqlite3
import threading
import time
import subprocess
from typing import List
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager

import serial
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# ============================================================================
# App setup
# ============================================================================

VERSION = "1.0.2"
CODENAME = "Kusha"

# ============================================================================
# Configuration
# ============================================================================

DB_PATH = os.getenv("SMS_DB_PATH", "/app/data/sms.db")
SERIAL_PORT = os.getenv("SMS_SERIAL_PORT", "/dev/ttyACM0")
BAUDRATE = int(os.getenv("SMS_BAUDRATE", "115200"))

# Security settings
API_KEY = os.getenv("SMS_API_KEY", "")  # Set this in docker-compose/env
SSL_CERT_PATH = os.getenv("SMS_SSL_CERT", "/app/data/cert.pem")
SSL_KEY_PATH = os.getenv("SMS_SSL_KEY", "/app/data/key.pem")

# ============================================================================
# API Key Authentication
# ============================================================================

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key if one is configured."""
    if not API_KEY:
        # No API key configured - allow all requests (backward compatible)
        return True
    
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Include X-API-Key header."
        )
    
    if api_key != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid API key."
        )
    
    return True

# ============================================================================
# SSL Certificate Generation
# ============================================================================

def generate_self_signed_cert():
    """Generate self-signed SSL certificate if it doesn't exist."""
    cert_path = Path(SSL_CERT_PATH)
    key_path = Path(SSL_KEY_PATH)
    
    if cert_path.exists() and key_path.exists():
        print(f"[INFO] Using existing SSL certificate: {SSL_CERT_PATH}")
        return True
    
    print("[INFO] Generating self-signed SSL certificate...")
    
    try:
        # Generate using openssl
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:4096",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", "365",
            "-nodes",  # No passphrase
            "-subj", "/CN=sms-gateway/O=Local/C=NO"
        ], check=True, capture_output=True)
        
        print(f"[INFO] SSL certificate generated: {SSL_CERT_PATH}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to generate SSL certificate: {e}")
        return False
    except FileNotFoundError:
        print("[ERROR] openssl not found - cannot generate certificate")
        return False

# ============================================================================
# Application Lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    # Startup
    init_db()
    generate_self_signed_cert()
    if API_KEY:
        print(f"[INFO] API key authentication enabled")
    else:
        print(f"[WARN] No API key configured - all requests allowed")
    print(f"[INFO] Kusha {VERSION} started")
    yield
    # Shutdown
    print("[INFO] Kusha shutting down")

app = FastAPI(
    title="Kusha",
    version=VERSION,
    description="SMS Gateway API for iGate Prime GSM devices. Twin of Lava (SMS Web App).",
    lifespan=lifespan,
)

# ============================================================================
# Database helpers
# ============================================================================


def init_db() -> None:
    """Create messages table if it does not exist."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL,  -- 'in' or 'out'
            number TEXT NOT NULL,
            text TEXT NOT NULL,
            unread INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    con.commit()
    con.close()


def get_db_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


@contextmanager
def get_db():
    """Context manager for database connections."""
    con = sqlite3.connect(DB_PATH)
    try:
        yield con
    finally:
        con.close()


# ============================================================================
# Pydantic models
# ============================================================================


class HealthResponse(BaseModel):
    status: str = "ok"
    serial: str = "unknown"
    version: str = VERSION


class SMSRequest(BaseModel):
    number: str
    text: str


class SMSSendResponse(BaseModel):
    to: str
    text: str
    status: str  # "sent" or "error"
    error: str | None = None


class SMSMessage(BaseModel):
    id: int
    direction: str  # "in" or "out"
    number: str
    text: str
    unread: bool


class InboxMessage(BaseModel):
    number: str
    text: str


class InboxResponse(BaseModel):
    messages: List[InboxMessage]


# ============================================================================
# Serial port handling
# ============================================================================

ser: serial.Serial | None = None
serial_lock = threading.Lock()

# Flag to pause the reader thread during send operations
reader_paused = threading.Event()
reader_paused.clear()  # Not paused by default

# In-memory inbox for newly received messages (since last /sms/inbox)
inbox: list[dict[str, str]] = []
inbox_lock = threading.Lock()


def _init_serial_locked() -> serial.Serial:
    """
    Initialize serial port and modem.
    NOTE: Must be called with serial_lock already held.
    """
    global ser

    if ser is not None and ser.is_open:
        return ser

    ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)

    # Basic modem init
    time.sleep(0.5)
    ser.write(b"AT\r")
    ser.readline()

    # Text mode
    ser.write(b"AT+CMGF=1\r")
    time.sleep(0.5)
    ser.readline()

    # Enable indications (not strictly required for polling, but harmless)
    ser.write(b"AT+CNMI=2,1,0,0,0\r")
    time.sleep(0.5)
    ser.readline()

    # Flush input buffer
    ser.reset_input_buffer()

    print(f"[INFO] Serial port initialized on {SERIAL_PORT} @ {BAUDRATE}")
    return ser


def get_serial() -> serial.Serial:
    """Get an initialized serial object, with lazy initialization + locking."""
    with serial_lock:
        try:
            return _init_serial_locked()
        except Exception as e:
            print(f"[ERROR] Failed to initialize serial port: {e}")
            raise


# ============================================================================
# Incoming SMS handling
# ============================================================================


def _store_incoming_sms(number: str, text: str) -> None:
    """Store incoming SMS in DB and in-memory inbox."""
    msg = {"number": number, "text": text}

    # Push to in-memory inbox
    with inbox_lock:
        inbox.append(msg)

    # Persist to DB
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO messages (direction, number, text, unread) VALUES (?, ?, ?, ?)",
            ("in", number, text, 1),
        )
        con.commit()
        con.close()
        print(f"[INFO] Stored incoming SMS from {number!r}: {text!r}")
    except Exception as e:
        print(f"[DB ERROR] Failed to insert incoming SMS: {e}")


def fetch_unread_messages() -> None:
    """
    Poll modem for SMS and store them.

    Uses AT+CMGL="ALL" to fetch all messages, then deletes them
    after importing into the DB + inbox.
    """
    global ser
    
    # Ask for ALL messages (REC READ, REC UNREAD, etc.)
    with serial_lock:
        try:
            # Initialize serial if needed
            if ser is None or not ser.is_open:
                ser = _init_serial_locked()
            s = ser
        except Exception as e:
            print(f"[ERROR] fetch_unread_messages: cannot get serial: {e}")
            return

        try:
            s.write(b'AT+CMGL="ALL"\r')
            time.sleep(0.5)

            lines: list[str] = []
            # Read until "OK" or timeout; ignore blank lines
            for _ in range(400):
                raw = s.readline()
                if not raw:
                    break
                l = raw.decode(errors="ignore").strip()
                if not l:
                    # Skip empty spacer lines between messages
                    continue
                lines.append(l)
                if l == "OK":
                    break
        except Exception as e:
            print(f"[ERROR] fetch_unread_messages: failed to read CMGL output: {e}")
            return

    if not lines:
        return

    print("[DEBUG] CMGL response:")
    for l in lines:
        print("   ", repr(l))

    # Example format (your modem):
    # +CMGL: 2,"REC READ","+4712345678",,"25/11/20,01:11:57+04"
    # Test
    # +CMGL: 3,"REC READ","+4712345678",,"25/11/20,01:22:35+04"
    # Test01
    # OK

    current_index: int | None = None
    current_number: str = "unknown"
    current_body_lines: list[str] = []
    found_indices: list[int] = []

    def flush_current() -> None:
        nonlocal current_index, current_number, current_body_lines
        if current_index is None:
            return
        text = "\n".join(current_body_lines).strip() or "(empty)"
        _store_incoming_sms(current_number, text)
        found_indices.append(current_index)
        current_index = None
        current_number = "unknown"
        current_body_lines = []

    for line in lines:
        if line.startswith("+CMGL:"):
            # New header → flush previous one
            flush_current()

            try:
                # Drop "+CMGL:" and split the rest
                header = line.split(":", 1)[1].strip()
                parts = [p.strip() for p in header.split(",")]

                # parts[0] = index
                current_index = int(parts[0])

                # parts[2] = number: "+4712345678"
                if len(parts) > 2:
                    current_number = parts[2].strip('" ')
                else:
                    current_number = "unknown"
            except Exception as e:
                print(f"[WARN] Failed to parse CMGL header: {line!r} ({e})")
                current_index = None
                current_number = "unknown"
                current_body_lines = []
        elif line == "OK":
            # "OK" could be part of the SMS body (e.g., iGate responding "OK")
            # Include it in the message body if we're currently parsing one
            if current_index is not None:
                current_body_lines.append(line)
            # End of CMGL output - break after potentially adding to body
            break
        else:
            # Body line (SMS text)
            if current_index is not None:
                current_body_lines.append(line)

    # Flush last pending message
    flush_current()

    if not found_indices:
        return

    # Try to delete imported messages individually from modem
    with serial_lock:
        try:
            # Use existing serial connection
            if ser is None or not ser.is_open:
                ser = _init_serial_locked()
            s = ser
            
            delete_failures = 0
            for idx in found_indices:
                try:
                    cmd = f"AT+CMGD={idx}\r".encode()
                    s.write(cmd)
                    time.sleep(0.2)
                    response = s.readline().decode(errors="ignore").strip()
                    if "ERROR" in response:
                        delete_failures += 1
                        print(f"[WARN] Failed to delete message index {idx}: {response}")
                    else:
                        print(f"[DEBUG] Deleted message index {idx}")
                except Exception as e:
                    delete_failures += 1
                    print(f"[WARN] Failed to delete message index {idx}: {e}")
            
            # If individual deletes failed, try bulk delete as fallback
            if delete_failures > 0:
                print(f"[WARN] {delete_failures} individual deletes failed, trying bulk delete")
                s.write(b"AT+CMGD=1,4\r")
                time.sleep(0.5)
                response = s.readline().decode(errors="ignore").strip()
                print(f"[DEBUG] Bulk delete response: {response}")
                
        except Exception as e:
            print(f"[ERROR] Failed to delete processed messages: {e}")



def serial_reader() -> None:
    """
    Background thread that periodically polls for unread messages.

    This thread will pause when reader_paused event is set (during send operations).
    """
    print("[INFO] serial_reader started (polling mode).")
    while True:
        try:
            # Check if paused - do this check frequently
            if reader_paused.is_set():
                time.sleep(0.2)
                continue
            
            fetch_unread_messages()
        except Exception as e:
            print(f"[ERROR] serial_reader loop error: {e}")
        time.sleep(10)  # poll every 10 seconds (increased from 5 to reduce interference)


# ============================================================================
# Routes
# ============================================================================


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Health check - no auth required."""
    serial_status = "connected" if (ser is not None and ser.is_open) else "disconnected"
    return HealthResponse(
        status="ok" if serial_status == "connected" else "degraded",
        serial=serial_status,
        version=VERSION
    )


@app.get("/version")
def get_version():
    """Return API version info."""
    return {
        "version": VERSION,
        "codename": CODENAME,
        "name": "Kusha",
        "twin": "Lava (SMS Web App)"
    }


@app.post("/sms/messages", response_model=SMSSendResponse)
def send_sms(payload: SMSRequest, _: bool = Depends(verify_api_key)) -> SMSSendResponse:
    """Send an SMS via the modem (with proper '>' prompt handling)."""
    if not payload.number or not payload.text:
        raise HTTPException(
            status_code=400,
            detail="Both 'number' and 'text' are required",
        )

    print(f"[DEBUG] send_sms called: number={payload.number}, text={payload.text[:50]}...")
    
    at_cmd = f'AT+CMGS="{payload.number}"\r'
    resp_lines: list[str] = []

    try:
        with serial_lock:
            # Initialize serial port if needed
            global ser
            try:
                if ser is None or not ser.is_open:
                    print("[DEBUG] Initializing serial port")
                    ser = _init_serial_locked()
                s = ser
                print(f"[DEBUG] Got serial port, is_open={s.is_open}, in_waiting={s.in_waiting}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Serial port not available: {e}")
            # Flush any leftover input
            try:
                if s.in_waiting > 0:
                    discarded = s.read(s.in_waiting)
                    print(f"[DEBUG] Discarded {len(discarded)} bytes from buffer")
                s.reset_input_buffer()
            except Exception as e:
                print(f"[WARN] Failed to reset input buffer: {e}")

            # Make sure we are in text mode
            try:
                s.write(b"AT+CMGF=1\r")
                time.sleep(0.3)
                # Drain any response lines
                for _ in range(5):
                    l = s.readline()
                    if not l:
                        break
                print("[DEBUG] Text mode confirmed")
            except Exception as e:
                print(f"[WARN] Failed to set text mode before send: {e}")

            # 1) Send AT+CMGS and wait for '>' prompt
            print(f"[DEBUG] Sending CMGS command: {at_cmd.strip()}")
            s.write(at_cmd.encode())
            prompt_seen = False
            start_time = time.time()
            timeout = 10  # 10 second timeout

            while time.time() - start_time < timeout:
                if s.in_waiting > 0:
                    raw = s.readline()
                    line = raw.decode(errors="ignore").strip()
                    print(f"[DEBUG] CMGS pre-prompt line: {repr(line)}")
                    
                    if ">" in line:
                        prompt_seen = True
                        print("[DEBUG] Prompt '>' detected!")
                        break
                    
                    resp_lines.append(line)
                    if "+CMS ERROR" in line:
                        print(f"[DEBUG] CMS ERROR detected: {line}")
                        break
                else:
                    time.sleep(0.1)

            if not prompt_seen and not any("+CMS ERROR" in l for l in resp_lines):
                error_text = "\n".join(resp_lines) or "No '>' prompt from modem (timeout)"
                print(f"[ERROR] Timeout waiting for '>': {error_text}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Modem timeout waiting for prompt: {error_text}",
                )

            if any("+CMS ERROR" in l for l in resp_lines):
                error_text = "\n".join(resp_lines)
                print(f"[ERROR] Modem returned CMS error before text: {error_text}")
                status = "error"
                error = error_text
            else:
                # 2) Send the text followed by Ctrl+Z
                print(f"[DEBUG] Sending SMS text: {payload.text[:50]}...")
                s.write(payload.text.encode() + b"\x1A")

                # 3) Read modem response (OK / +CMS ERROR) with timeout
                start_time = time.time()
                timeout = 15  # 15 seconds for message send
                
                while time.time() - start_time < timeout:
                    if s.in_waiting > 0:
                        raw = s.readline()
                        line = raw.decode(errors="ignore").strip()
                        if not line:
                            continue

                        resp_lines.append(line)
                        print(f"[DEBUG] CMGS response line: {repr(line)}")

                        if "OK" in line or "+CMS ERROR" in line:
                            break
                    else:
                        time.sleep(0.1)

                status = "sent" if any("OK" in l for l in resp_lines) else "error"
                error = None
                if status == "error":
                    error = "\n".join(resp_lines) or "Unknown modem error after CMGS"
                    print(f"[ERROR] SMS send failed: {error}")
                else:
                    print(f"[INFO] SMS sent successfully to {payload.number}")

        # Store outgoing SMS in DB
        try:
            con = get_db_connection()
            cur = con.cursor()
            cur.execute(
                "INSERT INTO messages (direction, number, text, unread) VALUES (?, ?, ?, ?)",
                ("out", payload.number, payload.text, 0),
            )
            con.commit()
            con.close()
        except Exception as db_e:
            print(f"[DB ERROR] Failed to insert outgoing SMS: {db_e}")

        print("[DEBUG] SMS send complete")

        return SMSSendResponse(
            to=payload.number,
            text=payload.text,
            status=status,
            error=error,
        )

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to send SMS: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {e}")


@app.get("/sms/messages", response_model=List[SMSMessage])
def list_messages(_: bool = Depends(verify_api_key)) -> List[SMSMessage]:
    """Return all stored SMS messages (incoming + outgoing)."""
    con = get_db_connection()
    cur = con.cursor()
    cur.execute("SELECT id, direction, number, text, unread FROM messages ORDER BY id DESC")
    rows = cur.fetchall()
    con.close()

    messages: List[SMSMessage] = []
    for row in rows:
        id_, direction, number, text, unread = row
        messages.append(
            SMSMessage(
                id=id_,
                direction=direction,
                number=number,
                text=text,
                unread=bool(unread),
            )
        )
    return messages


@app.get("/sms/inbox", response_model=InboxResponse)
def get_inbox(_: bool = Depends(verify_api_key)) -> InboxResponse:
    """
    Fetch new messages from the modem and return all unread incoming messages.
    
    This checks the modem for new messages, then returns ALL unread incoming
    messages from the database (not just newly fetched ones). This ensures
    messages aren't lost if a previous fetch stored them but they weren't
    delivered to a client.
    """
    # Fetch any new messages from modem first
    try:
        fetch_unread_messages()
    except Exception as e:
        print(f"[ERROR] Failed to fetch messages in get_inbox: {e}")
    
    # Clear the in-memory inbox (we'll use database instead)
    with inbox_lock:
        inbox.clear()
    
    # Get all unread incoming messages from database
    messages = []
    message_ids = []
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute(
            "SELECT id, number, text FROM messages WHERE direction = 'in' AND unread = 1 ORDER BY id ASC"
        )
        rows = cur.fetchall()
        
        for row in rows:
            msg_id, number, text = row
            messages.append(InboxMessage(number=number, text=text))
            message_ids.append(msg_id)
        
        # Mark these messages as read
        if message_ids:
            placeholders = ','.join('?' * len(message_ids))
            cur.execute(f"UPDATE messages SET unread = 0 WHERE id IN ({placeholders})", message_ids)
            con.commit()
            print(f"[INFO] Returned and marked {len(message_ids)} messages as read")
        
        con.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to get unread messages: {e}")
    
    return InboxResponse(messages=messages)


@app.delete("/sms/messages/{message_id}")
def delete_message(message_id: int, _: bool = Depends(verify_api_key)) -> dict:
    """Delete a specific message from the database by ID."""
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        deleted = cur.rowcount
        con.commit()
        con.close()
        
        if deleted == 0:
            raise HTTPException(status_code=404, detail=f"Message {message_id} not found")
        
        print(f"[INFO] Deleted message ID {message_id}")
        return {"status": "deleted", "id": message_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Failed to delete message {message_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete message: {e}")


@app.delete("/sms/messages")
def delete_all_messages(_: bool = Depends(verify_api_key)) -> dict:
    """Delete ALL messages from the database."""
    try:
        con = get_db_connection()
        cur = con.cursor()
        cur.execute("DELETE FROM messages")
        deleted = cur.rowcount
        con.commit()
        con.close()
        
        print(f"[INFO] Deleted all {deleted} messages from database")
        return {"status": "deleted", "count": deleted}
    except Exception as e:
        print(f"[ERROR] Failed to delete all messages: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete messages: {e}")
