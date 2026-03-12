# Kusha v1.0.2

SMS Gateway API for iGate Prime GSM devices. Twin of Lava (SMS Web App).

## Version History

### v1.0.2 - December 18, 2025
- **Fixed:** `/sms/inbox` now returns all unread messages from database, not just newly fetched ones
- Previously, if a message was fetched but not delivered (network issue, timing, etc.), it would be lost
- Messages are now reliably delivered even if they were stored by a previous fetch

### v1.0.1 - December 18, 2025
- **Fixed:** Messages now marked as read (`unread=0`) in database after being delivered via `/sms/inbox`
- Previously, all incoming messages stayed `unread=1` forever in the database

### v1.0.0 - Initial Release
- Self-signed SSL certificate (auto-generated)
- Optional API key authentication
- SQLite message storage
- Serial modem support

## Features
- Self-signed SSL certificate (auto-generated)
- Optional API key authentication
- SQLite message storage
- Serial modem support

## Quick Start

```bash
# Extract
tar -xzf kusha-1.0.0.tar.gz

# Build
docker build -t kusha .

# Run (basic - no auth, HTTPS enabled)
docker run -d \
  --name kusha \
  --restart unless-stopped \
  --device=/dev/ttyACM0 \
  -v /path/to/data:/app/data \
  -p 6969:6969 \
  kusha

# Run (with API key)
docker run -d \
  --name kusha \
  --restart unless-stopped \
  --device=/dev/ttyACM0 \
  -v /path/to/data:/app/data \
  -p 6969:6969 \
  -e SMS_API_KEY="your-secret-key-here" \
  kusha
```

## Docker Compose (Recommended)

```bash
# Clone/extract the files
cd kusha-1.0.0

# Set your API key (optional)
export SMS_API_KEY="your-secret-key-here"

# Build and run
docker-compose up -d --build

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SMS_API_KEY` | (empty) | API key for authentication. If empty, no auth required. |
| `SMS_DB_PATH` | `/app/data/sms.db` | SQLite database path |
| `SMS_SERIAL_PORT` | `/dev/ttyACM0` | Serial port for modem |
| `SMS_BAUDRATE` | `115200` | Serial baud rate |
| `SMS_SSL_CERT` | `/app/data/cert.pem` | SSL certificate path |
| `SMS_SSL_KEY` | `/app/data/key.pem` | SSL private key path |
| `SMS_DISABLE_SSL` | `false` | Set to `true` to run HTTP instead of HTTPS |

## API Endpoints

All endpoints except `/health` and `/version` require `X-API-Key` header if `SMS_API_KEY` is set.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check (no auth) |
| GET | `/version` | Version info (no auth) |
| POST | `/sms/messages` | Send SMS |
| GET | `/sms/messages` | List all messages |
| GET | `/sms/inbox` | Get new messages from modem |
| DELETE | `/sms/messages/{id}` | Delete specific message |
| DELETE | `/sms/messages` | Delete all messages |

## Example Request

```bash
# With API key
curl -k -X POST https://192.168.180.38:6969/sms/messages \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key-here" \
  -d '{"number": "+4712345678", "text": "Hello!"}'

# -k flag accepts self-signed certificate
```

## SSL Certificate

The container auto-generates a self-signed certificate on first start. The certificate is stored in `/app/data/` and persists across restarts if you mount the volume.

To use your own certificate, mount files to:
- `/app/data/cert.pem`
- `/app/data/key.pem`

## Changes from SMS Gateway v0.6.0

- Renamed from "SMS Gateway" to "Kusha" - twin of Lava (SMS Web App)
- Added docker-compose.yml for easier deployment
- Added `/version` endpoint
- Enhanced `/health` endpoint with serial port status
- Added `created_at` timestamp to messages table
- Replaced deprecated startup event with lifespan context manager
- Added database context manager helper
- Added VERSION_LOCATIONS.md for version tracking

## Troubleshooting

### Serial Port Permission Denied

If you see `Permission denied: '/dev/ttyACM0'`, the container user needs access to the serial device.

**Option 1: Check host dialout GID (recommended)**
```bash
# Find your host's dialout GID
stat -c '%g' /dev/ttyACM0

# If it's not 20, update docker-compose.yml:
group_add:
  - "YOUR_GID_HERE"  # Use the number from stat command
```

**Option 2: Run as root (quick fix)**
```yaml
# In docker-compose.yml, add:
user: root
```

**Option 3: Fix host permissions**
```bash
sudo chmod 666 /dev/ttyACM0  # Temporary, resets on reboot
```
