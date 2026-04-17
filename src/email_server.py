
import os
import sys
import time
import traceback
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, jsonify
from jinja2 import Environment, FileSystemLoader, select_autoescape, TemplateError
from google.auth.exceptions import DefaultCredentialsError
from pymongo import MongoClient

from logger import getJSONLogger
logger = getJSONLogger('emailservice-server')

env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html', 'xml'])
)
template = env.get_template('confirmation.html')

mongo_client = None
email_events_collection = None

# ── SMTP configuration (loaded once at startup) ───────────────────────────────
SMTP_HOST     = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER     = os.environ.get('SMTP_USER', '')          # your Gmail address
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')      # Gmail App Password
EMAIL_FROM    = os.environ.get('EMAIL_FROM', SMTP_USER)  # defaults to sender address

_smtp_enabled = bool(SMTP_USER and SMTP_PASSWORD)

if _smtp_enabled:
    logger.info(f'SMTP enabled — host={SMTP_HOST}:{SMTP_PORT}, user={SMTP_USER}')
else:
    logger.warning(
        'SMTP not configured (SMTP_USER / SMTP_PASSWORD missing). '
        'Emails will be LOGGED ONLY. Set the env variables to enable sending.'
    )
# ─────────────────────────────────────────────────────────────────────────────


def init_mongo_store():
    global mongo_client, email_events_collection
    mongo_uri = os.environ.get('EMAIL_MONGO_URI') or os.environ.get('MONGO_URI')
    if not mongo_uri:
        logger.info('Email Mongo persistence disabled.')
        return

    db_name   = os.environ.get('MONGO_DATABASE', 'notification_db')
    coll_name = os.environ.get('MONGO_EMAIL_EVENTS_COLLECTION', 'email_events')

    try:
        mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command('ping')
        email_events_collection = mongo_client[db_name][coll_name]
        email_events_collection.create_index([('orderId', 1)], name='idx_order_id')
        email_events_collection.create_index([('createdAt', 1)], name='idx_created_at')
        logger.info(f'Email Mongo persistence enabled on {db_name}.{coll_name}')
    except Exception as exc:
        logger.warning(f'Email Mongo initialization failed, continuing without persistence: {exc}')
        mongo_client = None
        email_events_collection = None


def send_email_via_smtp(to_address: str, subject: str, html_body: str) -> bool:
    """Send an HTML email using the configured Gmail SMTP credentials.

    Returns True on success, False on failure.
    """
    if not _smtp_enabled:
        logger.info(f'[SMTP DISABLED] Would have sent "{subject}" to {to_address}')
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = to_address
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()          # upgrade to TLS (required by Gmail)
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, to_address, msg.as_string())
        logger.info(f'Email sent successfully to {to_address} (subject: {subject})')
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            'SMTP authentication failed. Make sure SMTP_USER and SMTP_PASSWORD '
            f'are correct and that you are using a Gmail App Password: {exc}'
        )
    except smtplib.SMTPException as exc:
        logger.error(f'SMTP error while sending to {to_address}: {exc}')
    except Exception as exc:
        logger.error(f'Unexpected error while sending email to {to_address}: {exc}')
    return False


def normalise_order(raw: dict) -> dict:
    """Convert the camelCase API order dict to the snake_case shape
    expected by the Jinja2 confirmation.html template.
    Fills safe defaults for every field so the template never raises
    an UndefinedError regardless of how sparse the payload is.
    """
    raw_cost    = raw.get('shippingCost', {})
    raw_addr    = raw.get('shippingAddress', {})
    raw_items   = raw.get('items', [])

    def normalise_item(it: dict) -> dict:
        inner = it.get('item', {})
        cost  = it.get('cost', {})
        return {
            'item': {
                'product_id': inner.get('productId', inner.get('product_id', 'N/A')),
                'quantity':   inner.get('quantity', ''),
            },
            'cost': {
                'units':         cost.get('units', 0),
                'nanos':         cost.get('nanos', 0),
                'currency_code': cost.get('currencyCode', cost.get('currency_code', '')),
            },
        }

    return {
        'order_id':            raw.get('orderId',            raw.get('order_id', '')),
        'shipping_tracking_id': raw.get('shippingTrackingId', raw.get('shipping_tracking_id', '')),
        'shipping_cost': {
            'units':         raw_cost.get('units', 0),
            'nanos':         raw_cost.get('nanos', 0),
            'currency_code': raw_cost.get('currencyCode', raw_cost.get('currency_code', '')),
        } if raw_cost else {},
        'shipping_address': {
            'street_address_1': raw_addr.get('streetAddress1', raw_addr.get('street_address_1', '')),
            'street_address_2': raw_addr.get('streetAddress2', raw_addr.get('street_address_2', '')),
            'city':             raw_addr.get('city', ''),
            'country':          raw_addr.get('country', ''),
            'zip_code':         raw_addr.get('zipCode', raw_addr.get('zip_code', '')),
        } if raw_addr else {},
        'items': [normalise_item(i) for i in raw_items],
    }


app = Flask(__name__)


@app.route('/send-confirmation', methods=['POST'])
def send_order_confirmation():
    data     = request.get_json()
    email    = data.get('email', '')
    order    = data.get('order', {})
    order_id = order.get('orderId', '')

    logger.info(f'Received order confirmation request for {email} (order {order_id})')

    # ── Persist event to MongoDB (optional) ──────────────────────────────────
    email_status = 'pending'
    if email_events_collection is not None:
        try:
            email_events_collection.insert_one({
                'orderId':  order_id,
                'email':    email,
                'status':   email_status,
                'template': 'confirmation.html',
                'requestPayload': {
                    'orderId':            order_id,
                    'shippingTrackingId': order.get('shippingTrackingId', ''),
                    'itemCount':          len(order.get('items', [])),
                },
                'createdAt': datetime.utcnow(),
            })
        except Exception as exc:
            logger.warning(f'Failed to persist email event: {exc}')

    # ── Normalise order dict to snake_case for the template ─────────────────
    order_ctx = normalise_order(order)

    # ── Render HTML template ──────────────────────────────────────────────────
    try:
        html_body = template.render(order=order_ctx)
    except TemplateError as exc:
        logger.error(f'Template rendering failed: {exc}')
        return jsonify({'error': 'template_error', 'details': str(exc)}), 500

    # ── Send email ────────────────────────────────────────────────────────────
    subject = f'Your HipsterShop Order Confirmation #{order_id}'
    success = send_email_via_smtp(email, subject, html_body)

    # ── Update MongoDB status ─────────────────────────────────────────────────
    if email_events_collection is not None and order_id:
        try:
            new_status = 'sent' if success else ('skipped' if not _smtp_enabled else 'failed')
            email_events_collection.update_one(
                {'orderId': order_id, 'email': email},
                {'$set': {'status': new_status, 'updatedAt': datetime.utcnow()}},
                sort=[('createdAt', -1)],
            )
        except Exception as exc:
            logger.warning(f'Failed to update email event status: {exc}')

    return jsonify({'sent': success, 'smtpEnabled': _smtp_enabled})


@app.route('/_healthz', methods=['GET'])
def health_check():
    return 'ok'


def initStackdriverProfiling():
    project_id = None
    try:
        project_id = os.environ["GCP_PROJECT_ID"]
    except KeyError:
        pass
    return


if __name__ == '__main__':
    logger.info('Starting the email service.')
    init_mongo_store()

    try:
        if "DISABLE_PROFILER" in os.environ:
            raise KeyError()
        else:
            logger.info("Profiler enabled.")
            initStackdriverProfiling()
    except KeyError:
        logger.info("Profiler disabled.")

    port = os.environ.get('PORT', "8080")
    logger.info("Listening on port: " + port)
    app.run(host='0.0.0.0', port=int(port))
