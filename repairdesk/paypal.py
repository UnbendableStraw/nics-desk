"""Minimal PayPal Invoicing v2 client.

Uses only the Python standard library (urllib) so it adds no dependencies and works on the
owner's Mac out of the box. It is **not** exercised in the build container (no network / no
credentials); the owner runs it first against PayPal **sandbox** credentials, then live.

Flow (per PayPal's REST Invoicing v2 API):
  1. OAuth2 client-credentials token  -> POST /v1/oauth2/token
  2. Create a draft invoice           -> POST /v2/invoicing/invoices
  3. Send it to the customer          -> POST /v2/invoicing/invoices/{id}/send

`cfg` is a plain dict with keys: paypal_client_id, paypal_secret, paypal_env
('sandbox'|'live'), paypal_currency (e.g. 'USD').
"""

import base64
import json
import urllib.request
import urllib.error


def api_base(cfg):
    return ("https://api-m.paypal.com" if cfg.get("paypal_env") == "live"
            else "https://api-m.sandbox.paypal.com")


def paypal_configured(cfg) -> bool:
    return bool(cfg.get("paypal_client_id") and cfg.get("paypal_secret"))


def _post(url, headers, data, timeout=25):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _err_detail(data, status):
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error_description") or ""
        details = data.get("details") or []
        if details and isinstance(details, list):
            extra = "; ".join(d.get("description", "") for d in details if isinstance(d, dict))
            if extra:
                msg = f"{msg} ({extra})" if msg else extra
        if msg:
            return f"PayPal: {msg}"
    return f"PayPal returned status {status}."


def get_access_token(cfg):
    """Returns (token, None) or (None, error_message)."""
    if not paypal_configured(cfg):
        return None, "PayPal isn't set up yet. Add your Client ID and Secret in Settings."
    auth = base64.b64encode(
        f"{cfg['paypal_client_id']}:{cfg['paypal_secret']}".encode()).decode()
    try:
        status, body = _post(
            api_base(cfg) + "/v1/oauth2/token",
            {"Authorization": "Basic " + auth,
             "Content-Type": "application/x-www-form-urlencoded"},
            b"grant_type=client_credentials")
        data = json.loads(body or b"{}")
        if status == 200 and data.get("access_token"):
            return data["access_token"], None
        return None, _err_detail(data, status)
    except urllib.error.HTTPError as e:
        return None, f"PayPal auth failed ({e.code}). Check your credentials and environment."
    except Exception as e:  # noqa: BLE001 - surface any connection problem to the owner
        return None, f"Couldn't reach PayPal: {e}"


def build_invoice_payload(invoice):
    """Map our normalized invoice dict to PayPal's v2 invoice JSON.

    invoice = {
      recipient_email, recipient_first, recipient_last, currency, shop_name, note,
      items: [{name, qty, amount}],
    }
    """
    cur = invoice.get("currency") or "USD"
    items = []
    for it in invoice.get("items", []):
        items.append({
            "name": (it.get("name") or "Item")[:200],
            "quantity": str(it.get("qty", 1)),
            "unit_amount": {"currency_code": cur, "value": f"{float(it.get('amount', 0)):.2f}"},
        })
    recipient = {
        "billing_info": {
            "email_address": invoice.get("recipient_email", ""),
            "name": {"given_name": invoice.get("recipient_first", ""),
                     "surname": invoice.get("recipient_last", "")},
        }
    }
    payload = {
        "detail": {"currency_code": cur, "note": invoice.get("note", "")},
        "primary_recipients": [recipient],
        "items": items,
    }
    if invoice.get("shop_name"):
        payload["invoicer"] = {"business_name": invoice["shop_name"][:300]}
    return payload


def create_and_send_invoice(cfg, invoice):
    """Create a draft invoice and send it to the customer.
    Returns (True, {id, view_url}) or (False, error_message)."""
    token, err = get_access_token(cfg)
    if err:
        return False, err
    base = api_base(cfg)
    hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        status, body = _post(base + "/v2/invoicing/invoices", hdr,
                             json.dumps(build_invoice_payload(invoice)).encode())
        data = json.loads(body or b"{}")
        if status not in (200, 201):
            return False, _err_detail(data, status)
        href = data.get("href", "")
        inv_id = data.get("id") or (href.rstrip("/").split("/")[-1] if href else "")
        if not inv_id:
            return False, "PayPal didn't return an invoice id."
        status2, body2 = _post(f"{base}/v2/invoicing/invoices/{inv_id}/send", hdr,
                               json.dumps({"send_to_recipient": True}).encode())
        if status2 not in (200, 202):
            return False, _err_detail(json.loads(body2 or b"{}"), status2)
        return True, {"id": inv_id, "view_url": invoice_view_url(cfg, inv_id)}
    except urllib.error.HTTPError as e:
        return False, f"PayPal error {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:  # noqa: BLE001
        return False, f"Couldn't reach PayPal: {e}"


def invoice_view_url(cfg, inv_id):
    host = ("https://www.paypal.com" if cfg.get("paypal_env") == "live"
            else "https://www.sandbox.paypal.com")
    return f"{host}/invoice/p/#{inv_id}"
