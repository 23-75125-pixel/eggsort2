# EggSort+

EggSort+ combines a Flask operations dashboard, YOLO/OpenCV inspection, a
serial bridge, and ESP32 firmware for classifying and routing eggs.

## Project structure

```text
app.py                    Flask routes and sorting orchestration
config.py                 Validated environment configuration
database.py               Schema compatibility and administrator bootstrap
extensions.py             Unbound Flask database and OAuth extensions
models.py                 Persistence models and serialization contracts
security.py               CSRF, security headers, and login throttling
validation.py             Reusable API input validation
camera_session.py         Camera capture and inference lifecycle
detection_service.py      YOLO model loading and frame inference
esp32_bridge.py           Host-side serial protocol and controller state
egg_standards.py          Weight classes and servo commands
templates/                Server-rendered dashboard pages
static/                   Shared browser assets and security integration
esp32/eggsort_controller/ ESP32 firmware
supabase/schema.sql        PostgreSQL/Supabase schema
test_*.py                 Unit and regression tests
```

The hardware-facing modules expose stable singleton interfaces used by
`app.py`. Configuration and validation are deliberately independent of Flask
so they can be tested without starting hardware or a web server.

## Local setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and replace all placeholder credentials.
4. Put the trained model at `best.pt`, or set `YOLO_MODEL_PATH`.
5. Start the web application with `python app.py`.

Never commit `.env`, model weights, runtime databases, or local virtual
environments. The repository's `.gitignore` excludes these files.

## Validation

Run the complete Python regression suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Compile the firmware for an ESP32 Dev Module:

```powershell
arduino-cli compile --fqbn esp32:esp32:esp32 esp32/eggsort_controller
```

## Security model

- Every unsafe browser request requires a session-bound CSRF token.
- Authentication attempts are throttled per normalized account identifier.
- Session cookies are HTTP-only, SameSite-protected, and secure on HTTPS.
- Dynamic responses include clickjacking, MIME-sniffing, referrer, permissions,
  and cache-control protections.
- Upload size, environment values, profile fields, and monetary inputs are
  bounded and validated before use.
- Invitation tokens are random, single-use, time-limited, and stored only as
  hashes in the database.

See `HARDWARE_SETUP.md`, `GOOGLE_AUTH_SETUP.md`, and `SUPABASE_SETUP.md` for
integration-specific setup.
