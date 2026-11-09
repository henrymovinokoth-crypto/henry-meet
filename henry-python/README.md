# Henry (Python edition)

Free video and voice meetings with paid upgrades by M-Pesa.

## Files
- app.py: the Python server (meetings, limits, payments)
- static/index.html: the website
- static/admin.html: your payments page at /admin
- requirements.txt, render.yaml: hosting setup

## Run on your computer
    pip install -r requirements.txt
    set ADMIN_PASSWORD=choose-a-password        (Windows)   |   export ADMIN_PASSWORD=choose-a-password   (Mac/Linux)
    uvicorn app:app --reload
Open http://localhost:8000 (camera works on localhost).

## Settings (environment variables)
- SECRET_KEY: long random text. Keep it the same forever, or customers' plan keys stop working.
- ADMIN_PASSWORD: password for /admin.
- PAY_NUMBER, PAY_NAME: shown to customers (default 0181288331, Henry Okoth).
- Optional automatic M-Pesa prompt (needs a Safaricom Paybill or Till): MPESA_KEY, MPESA_SECRET, MPESA_SHORTCODE, MPESA_PASSKEY, MPESA_TILL (only for Till), PUBLIC_URL (your site address), MPESA_ENV (sandbox or production).
- Optional TURN relay for hard networks: TURN_URL, TURN_USER, TURN_PASS.
