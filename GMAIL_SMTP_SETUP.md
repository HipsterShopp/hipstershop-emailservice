# Gmail SMTP Configuration Guide

This guide explains every step required to enable real email sending in the
`hipstershop-emailservice` via Gmail's SMTP relay.

---

## Why Gmail SMTP?

The service uses Python's built-in `smtplib` with **STARTTLS** (port 587) to
connect to `smtp.gmail.com`.  No additional Python packages are needed — the
existing `requirements.txt` is unchanged.

---

## Step 1 — Enable 2-Step Verification on your Google Account

Gmail App Passwords (used in Step 2) **require** 2-Step Verification to be
active first.

1. Go to [https://myaccount.google.com/security](https://myaccount.google.com/security).
2. Under **"How you sign in to Google"**, click **2-Step Verification**.
3. Follow the on-screen prompts to enable it (you only need to do this once).

---

## Step 2 — Generate a Gmail App Password

> [!IMPORTANT]
> Use an **App Password**, not your normal Gmail password.  Google blocks
> direct password sign-in for SMTP by default (since May 2022).

1. Go to [https://myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   (You must be signed in and 2-Step Verification must be ON.)
2. In the **"App name"** field type something like `hipstershop-email`.
3. Click **Create**.
4. Google will display a **16-character password** (e.g. `abcd efgh ijkl mnop`).
5. **Copy it immediately** — it is shown only once.
6. Remove any spaces when you use it (i.e. `abcdefghijklmnop`).

---

## Step 3 — Set the Environment Variables

The service reads its SMTP credentials from environment variables.  Set **all
four** variables before starting the service:

| Variable        | Description                              | Example value              |
|-----------------|------------------------------------------|----------------------------|
| `SMTP_HOST`     | Gmail SMTP server (default already set)  | `smtp.gmail.com`           |
| `SMTP_PORT`     | SMTP port with STARTTLS (default set)    | `587`                      |
| `SMTP_USER`     | **Your Gmail address**                   | `you@gmail.com`            |
| `SMTP_PASSWORD` | **Your Gmail App Password** (no spaces)  | `abcdefghijklmnop`         |
| `EMAIL_FROM`    | Display "From" address (defaults to `SMTP_USER`) | `HipsterShop <you@gmail.com>` |

### Running locally (PowerShell)

```powershell
$env:SMTP_USER     = "you@gmail.com"
$env:SMTP_PASSWORD = "abcdefghijklmnop"
$env:SMTP_HOST     = "smtp.gmail.com"
$env:SMTP_PORT     = "587"

python src/email_server.py
```

### Running with Docker

```bash
docker run \
  -e SMTP_USER="you@gmail.com" \
  -e SMTP_PASSWORD="abcdefghijklmnop" \
  -e SMTP_HOST="smtp.gmail.com" \
  -e SMTP_PORT="587" \
  -p 8080:8080 \
  hipstershop-emailservice
```

### Kubernetes — Secret + Deployment patch

1. **Create a Kubernetes Secret** (never put credentials in plain ConfigMaps):

```bash
kubectl create secret generic emailservice-smtp \
  --from-literal=SMTP_USER="you@gmail.com" \
  --from-literal=SMTP_PASSWORD="abcdefghijklmnop"
```

2. **Reference the secret in your Deployment** (add under `env:` in the
   `emailservice` container spec):

```yaml
env:
  - name: SMTP_HOST
    value: "smtp.gmail.com"
  - name: SMTP_PORT
    value: "587"
  - name: SMTP_USER
    valueFrom:
      secretKeyRef:
        name: emailservice-smtp
        key: SMTP_USER
  - name: SMTP_PASSWORD
    valueFrom:
      secretKeyRef:
        name: emailservice-smtp
        key: SMTP_PASSWORD
```

---

## Step 4 — Test the Service

Send a test request with `curl` (or any HTTP client):

```bash
curl -X POST http://localhost:8080/send-confirmation \
  -H "Content-Type: application/json" \
  -d '{
    "email": "recipient@example.com",
    "order": {
      "orderId": "TEST-001",
      "shippingTrackingId": "TRACK-XYZ",
      "items": []
    }
  }'
```

**Expected success response:**

```json
{"sent": true, "smtpEnabled": true}
```

Check your inbox at `recipient@example.com` — you should receive the
HipsterShop order confirmation email.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `{"sent": false, "smtpEnabled": false}` | Env vars not set | Set `SMTP_USER` and `SMTP_PASSWORD` |
| `SMTPAuthenticationError` in logs | Wrong password or 2FA not on | Re-generate App Password; confirm 2FA is enabled |
| `Connection refused` / timeout | Port 587 blocked | Check firewall / corporate proxy; try port 465 with SSL |
| `535 Username and Password not accepted` | Using account password instead of App Password | Generate an App Password (Step 2) |
| Email in **Spam** folder | No SPF/DKIM on custom domain | Use your Gmail `@gmail.com` address as sender, or configure SPF/DKIM |

---

## How the Code Works (brief overview)

```
POST /send-confirmation
       │
       ├─ Persist request to MongoDB (optional)
       │
       ├─ Render confirmation.html Jinja2 template with order data
       │
       ├─ send_email_via_smtp()
       │     ├─ Opens SMTP connection to smtp.gmail.com:587
       │     ├─ Upgrades to TLS (STARTTLS)
       │     ├─ Logs in with SMTP_USER / SMTP_PASSWORD
       │     └─ Sends MIMEMultipart HTML email
       │
       └─ Returns {"sent": true/false, "smtpEnabled": true/false}
```

If `SMTP_USER` / `SMTP_PASSWORD` are missing the service still starts and
responds to health checks — it just logs `[SMTP DISABLED]` instead of
sending, so existing Kubernetes liveness probes are never broken.
