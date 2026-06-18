# Nic's Desk

A self-hosted CRM for a repair shop, designed to run on **your Mac** so it can send
**native iMessages** to customers about their repairs. Everything — database, web
server, message sending — runs locally. No cloud, no subscription, your data stays
on your machine.

## What it does

- **Repair tickets** with a clean dashboard, search, and status workflow
  (Received → Diagnosing → Awaiting parts → In progress → Ready for pickup → Completed).
- **Per-device-type numbering** — define prefixes like `PH` (phones), `LT` (laptops),
  each with its own start number and running counter, so you get `PH-2001`, `LT-500`, etc.
- **Issue catalog** — define your common services with an estimated cost; pick one on a
  ticket and the quote auto-fills.
- **Asset tracking** — log a device's serial number against a customer, keep several per
  customer, and transfer one to a new owner when a device changes hands. Serials entered
  on a ticket are logged as assets automatically.
- **Customer updates by iMessage and/or email** — one click texts via Messages and/or
  emails from your own SMTP (e.g. a professional Gmail).
- **Fulfillment method** — record how a device comes and goes (drop-off / pick-up,
  ship-in / ship-back, mail-in / mail-back, on-site, etc.).
- **Customer portal** — customers check a single repair with their **repair number + last
  name** (no account), or **sign in with a one-time emailed link** to see *all* their repairs
  in one place and submit new ones without re-entering their details.
- **QR + barcode labels** — print a sticker per repair. Scan the **QR with your phone**:
  if you're signed in it opens the full ticket; if a customer scans it, they see status.
  A **Code 128 barcode** is also on the label for USB/Bluetooth scanners.
- **Google Sheets import** — bring in your existing customers from a CSV export.

Upgrading from an earlier version? Just replace the files and keep your `repairdesk.db` —
the app migrates the database on first run and preserves your data and numbering.

---

## Setup (Mac)

You need Python 3 (macOS includes it; if missing, install from python.org or `brew install python`).

1. Unzip this folder somewhere, e.g. your home directory.
2. Open **Terminal**, `cd` into the folder, and run:

   ```bash
   ./run.sh
   ```

   The first run creates a virtual environment and installs Flask, qrcode, etc.
   (You may need to make it executable once: `chmod +x run.sh`.)

3. Open **http://localhost:5050** in your browser.
   Sign in with username **`owner`** and password **`changeme`** — change the password in
   **Settings** immediately, and add accounts for your staff under **Users**.

   > Upgrading from a single-password version? Your old password still works — just sign in
   > as username **`owner`** with it. You'll then see a **Users** page to add Managers and
   > Technicians and give everyone their own login.

To run it manually instead of the script:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

> The app uses port **5050** (not 5000, which macOS reserves for AirPlay).

---

## Turning on iMessage sending

iMessage sending works only on macOS with the **Messages** app signed into your Apple ID.

1. Open **Messages** and sign in (you can send/receive a test iMessage to confirm).
2. The first time the app sends a message, macOS will ask to allow **Terminal**
   (or whatever you launched it from) to control **Messages** — click **OK**.
   If you miss the prompt, enable it under
   **System Settings → Privacy & Security → Automation → Terminal → Messages**.
3. If sending fails with an authorization error, the message will tell you; re-grant
   the Automation permission above.

Off a Mac, the app still runs — messages are logged to the terminal instead of sent,
so you can develop and test everything else.

If your macOS version dislikes the bundled AppleScript, you can edit
`send_imessage.applescript` (a couple of known-good variations exist online for
different macOS releases).

---

## Sending email updates (SMTP / Gmail)

You can notify customers by email in addition to (or instead of) iMessage, sent from
your own address. Set it up under **Settings → Email (SMTP)**.

For a **professional Gmail / Google Workspace** account:

1. Turn on **2-Step Verification** for the account (Google Account → Security).
2. Create an **App Password**: Google Account → Security → 2-Step Verification →
   **App passwords**. You'll get a 16-character password.
3. In RepairDesk Settings:
   - **Enable email sending** ✓
   - Host `smtp.gmail.com`, Port `587`, Security **STARTTLS**
   - Username: your full Gmail address
   - **App password**: the 16-character one (not your normal password)
   - From address: e.g. `My Shop <you@yourdomain.com>`
4. Edit the **email subject** and **body** templates if you like. Available placeholders:
   `{first_name}` `{last_name}` `{repair_number}` `{device}` `{serial}` `{status}`
   `{issue_name}` `{price}` `{fulfillment}` `{track_url}` `{shop_name}`.

Now each status update shows **iMessage** and **Email** checkboxes, and the per-ticket
message composer lets you pick a channel. (The SMTP password is stored in your local
`repairdesk.db`; that file stays on your Mac. Use an App Password so you can revoke it
anytime without changing your main password.)

**Message templates**: the **Templates** page (Owners/Managers) holds reusable messages —
RepairDesk ships with a few starters (Received, Estimate ready, Waiting on parts, Ready for
pickup) you can edit or delete. In a ticket's **Send a message** box, pick a template from the
dropdown and it fills the text with this repair's details substituted in; edit before sending.
Templates use the same placeholders as the auto-update messages (`{first_name}`, `{device}`,
`{repair_number}`, `{status}`, `{total}`, `{track_url}`, `{shop_name}`, …). Any active template
is available on every ticket, for both iMessage and email.

---

## Numbering, issues & assets

**Device types & repair numbers** (Settings → *Device types & repair numbers*): create a
prefix per kind of device — `PH` Phone, `LT` Laptop, `TB` Tablet — each with its own
**start number** and independent counter. Tickets then read `PH-2001`, `LT-500`, and so on.
Mark one type as the **★ default** (used for customer-submitted requests). You can rename a
type or set its next number anytime; a type that's already on real tickets can't be deleted.

**Issues** (Issues page): build a catalog of common services, each with an estimated cost
(e.g. *Screen replacement — $129*). On a new or existing repair, pick an issue from the
dropdown and the quote auto-fills (you can still override it). Deleting an issue that's in
use deactivates it instead, so history stays intact.

**Assets** (Assets page): log a customer's device by **serial number**. A customer can have
several assets, and you can **transfer** one to a different customer when a device is sold or
handed off. Entering a serial on a repair ticket automatically creates/links the asset, so
the catalog fills itself as you work.

**Fulfillment**: every ticket has two method fields — how the device gets **to you**
(drop-off, ship-in, etc.) and how it gets **back** to the customer (pick-up, ship-back,
etc.). Both appear on the dashboard, the ticket, and the customer's status page. You set
the available options, their costs, which devices they apply to, and whether they need a
mailing address on the **Shipping** page (see *Shipping options* below).

---

## Devices & multi-issue tickets

**Devices** (Devices page): build a catalog of the models you service — *iPhone 13*,
*MacBook Air M2* — and tick which **Issues** apply to each. When a customer picks a device
on the request form, they see exactly those issues with prices, which drives their estimate.
(Devices are the *models you fix*; Assets are *specific units with serial numbers*; prefixes
are *numbering buckets*.)

**Multiple issues per repair**: a ticket can carry several services at once. On the ticket,
the *Issues on this ticket* panel lets you add or remove issues, shows the running estimate,
and a **Use as quote** button drops that total into the price field. The new-repair form has
the same multi-select with a live price.

**Editing & deleting**: open any ticket to edit the customer, device, serial, shipping
methods, notes, and quote. The **Danger zone** deletes a ticket (with confirmation). Open a
customer from the Customers list to edit their details or delete them — deletion is blocked
while they still have repairs, so nothing is orphaned.

---

## Addresses, shipping labels & estimates

**Customer addresses**: customers and the request form capture a mailing address
(address 1/2, city, state, ZIP, country). It's needed for shipping labels and shows on
the ticket's Details panel, where you can edit it.

**Running estimated total**: a ticket no longer has a single typed quote. Its **Estimated
total** is calculated live from the attached issues, additional services, and the cost of
the chosen shipping methods, and is shown as a breakdown on the ticket and on the
customer's status page. Adjust the figure by adding/removing issues or services, or by
setting method costs in Settings.

**Additional Services** (Services page): a catalog of optional extras — deep clean, custom
color, mailing a packing kit, express turnaround, etc. — each with a cost. Customers can
add these on the request form, and you can add/remove them on a ticket; either way they
feed the running total.

**Issue ↔ device links from either side**: associate devices with an issue on the Issues
page, or issues with a device on the Devices page — it's the same relationship, editable
from whichever is convenient.

**Shipping labels (you upload them)**: you create labels yourself (USPS, UPS, PayPal —
whatever you use) and upload them on the repair ticket. A PDF is treated as a printable
label; an image (PNG/JPG, e.g. a QR code) is treated as a scannable label. Add an optional
tracking number and carrier, then text or email it to the customer straight from the ticket.
The message wording is set under Settings → Shipping labels. On the request form, customers
can still say whether they'd prefer a printable PDF or a scannable QR (for inbound options
you've flagged with the "QR" toggle in Settings) — that preference shows on the ticket so you
know which kind of label to upload.

**Devices link to issues *and* services**: each device model is linked to the issues and the
additional services that apply to it. When a customer picks a device on the request form,
they see exactly that device's issues and add-on services, each priced, feeding the running
estimate. Associations are managed with a searchable **tag picker** (type to find an item,
click to add, ✕ to remove) — on the Devices page (a device's issues and services), the
Issues page (an issue's devices), and the Services page (a service's devices). They're the
same links viewed from either side, so editing from whichever page is handy stays consistent.

**Request-form label choice**: in Settings you can flag any inbound option as a *shipping*
option (the "label" checkbox). When a customer picks one of those on the request form, they
must choose how they want their label — printable PDF or scannable QR — and that choice is
saved on the ticket. The form also has an **Additional comments** box, saved to the ticket
notes.

**Cost ranges**: an issue's or service's cost can be a single price (`$129`) or a range
(`$50-70`). Ranges flow all the way through — the customer's running estimate and the
ticket's estimate show as a range (e.g. *$65–$90*) by summing the low ends and high ends
separately. Enter either format in the cost box.

**Photos / GIFs / audio on issues**: on the Issues page you can upload images, animated
GIFs, or audio clips (MP3/WAV/OGG) to any issue. When a customer picks a device on the
request form, each issue shows its pictures/GIFs and an audio player, so they can see or
hear what the problem looks/sounds like before choosing it.

**Estimated delivery days on shipping**: each inbound and outbound option has optional
"days" boxes in Settings (e.g. 2–3). When set, the estimate appears in the customer's
shipping dropdown (e.g. *Ship In with USPS Priority (+$8) · est. 2–3 days*).

**Response & turnaround times**: set "Current estimated response time" and "Current
estimated turnaround time" under Settings → Customer-facing info. They show at the top of
the new-request page so customers know when to expect a reply and their device back.

**"More than X devices repaired" counter**: the request and tracking pages can show a
*More than X devices repaired, and counting!* banner. The count is the sum of every device repair
type's counter.

**Editable statuses**: the repair stages (Received, Diagnosing, etc.) live under Settings →
Repair statuses. Rename, recolor, reorder, add, or remove them — renaming updates existing
tickets automatically, and you can't delete a status that tickets are currently in. You also
choose which status new staff tickets vs. customer requests start in.

**Issue descriptions**: issues have a description field (on the Issues page), shown to
customers on the request form. Handy when several devices share an issue name ("Screen") at
different prices/specs.

**Device-based ticket numbers**: each device in the catalog can be tied to a ticket prefix
(Devices page). A request for that device is numbered from its prefix — pick "AHP" for Apple
HomePod and its tickets become `AHP-5005`. "Other / not listed" falls back to the default
prefix, and a multi-device request still yields one number per device.

**Asset photos**: upload labeled condition photos to an asset (Assets page) — e.g. "On
arrival — back" — with a gallery and per-photo delete.

---

## Staff accounts & roles

Each person who works in RepairDesk gets their own **username and password**, managed on
the **Users** page (visible to Owners). Every account has one of three **roles**:

- **Owner** — full access, including managing staff accounts and shop settings.
- **Manager** — everything except managing staff accounts: repairs, customers, the catalogs
  (issues, services, devices, shipping, statuses, numbering) and settings.
- **Technician** — the day-to-day work: create and edit tickets, change status, attach
  issues/services, upload and send labels, manage assets, and message customers. Technicians
  can't change catalogs or settings, manage users, delete tickets/customers, or import.

The nav and on-page controls adjust to each role, and every action is also enforced on the
server, so a Technician who guesses a URL still can't perform a blocked action.

Change **your own** password under **Settings → Staff password**. Owners can reset anyone
else's password (and change roles or deactivate accounts) on the **Users** page. RepairDesk
always keeps at least one active Owner, so you can't accidentally lock yourself out.

---

## Shipping options

The **Shipping** page (Owners and Managers) is where you define how devices come **to you**
(inbound) and go **back** (outbound). Each option has:

- A **cost** (folded into the customer's estimate; leave 0 for free) and an optional
  **estimated-days** range shown in the customer's dropdown.
- **Needs a shipping label** / **Can use QR label** — the same flags as before, controlling
  the label workflow and whether the customer is offered a printable PDF vs a scannable QR.
- **Needs mailing address** — when a customer picks an option with this on, the request form
  reveals and **requires** the address fields (address, city, state, ZIP). Off by default for
  in-person options like drop-off/pickup; on for ship-in/ship-back.
- **Linked devices** — leave empty and the option applies to **every** device. Add one or
  more devices to restrict it: the customer request form then only offers that option when
  **all** of the devices they've chosen support it. (Pick "Other / not listed" and there's no
  restriction.) Staff tickets always show every active option, so you can override.

---

## Customer portal (email sign-in)

Customers always have the no-account path — **check one repair** at `/track` with a repair
number and last name. On top of that, you can let returning customers **sign in** to see all
their repairs at once.

**How it works**: a customer enters their email at `/portal`; if it matches a customer on
file, RepairDesk emails them a **one-time sign-in link** (no password). The link works once
and expires after 30 minutes. Once signed in they see every repair tied to their email, the
devices you've logged for them, and a **New repair** button that submits without re-entering
their name and contact details (it reuses their existing record).

**Turning it on**: Settings → *Customer portal (email sign-in)*. It needs **email (SMTP)**
set up first (same Gmail App Password setup as status emails). Tick **Enable the customer
portal**, and customize the sign-in email if you like — placeholders `{first_name}`,
`{link}` (required), `{minutes}`, `{shop_name}`. Share `http://<your-mac-ip>:5050/portal`.

**Security notes**: the link is a single-use, 30-minute token; requesting a new one cancels
any earlier unused link. The page gives the same "check your email" response whether or not
the address matches, so it can't be used to discover who's a customer. If email isn't set up,
sign-in is unavailable and customers fall back to repair-number + last-name lookup. *(For a
shop on the open internet, put HTTPS in front — see "Letting customers reach it".)*

> Testing without email configured? The sign-in link is also printed to the terminal where
> the app runs, so you can copy it during setup before SMTP is wired up.

**Condition photos**: any photos you attach to a device on the **Assets** page (e.g. "On
arrival — back") show up for the customer on their repair's status page and in the portal —
served through a link scoped to that one repair, so only someone holding the repair's link
sees its photos.

**Warranty repairs**: on any past (non-warranty) repair, a signed-in customer can hit
**Warranty** to start a follow-up request. It creates a fresh ticket that reuses the same
device and asset and links back to the original — both tickets cross-link on the staff side,
and the new one starts in your customer-intake status for you to triage.

**Per-repair chat**: a signed-in customer and the shop can message back and forth on a
specific repair. Staff post from the ticket's **Customer chat** box; the customer replies on
their repair's status page (only while signed in — anonymous link-holders never see the
thread). It's separate from one-off iMessage/email updates, and each post is also noted on the
ticket's internal activity log.

**Reading replies (iMessage + email)**: RepairDesk can pull customers' replies onto the
matching ticket's chat, so the whole conversation lives in one place. Turn it on in
**Settings → Reading customer replies**:

- *iMessage* — reads this Mac's Messages history (`~/Library/Messages/chat.db`). Grant the app
  **Full Disk Access** (System Settings → Privacy & Security). Mac only.
- *Email* — polls an IMAP mailbox for unread mail (Gmail: `imap.gmail.com` with an App
  Password). Imported mail is marked read.

Click **Check replies** on the Repairs page to pull new ones (or schedule a `launchd`/`cron`
job that POSTs to `/inbound/poll`). Each reply is matched to a customer by phone or email and
to a ticket by any repair number in the message — otherwise their most recent ticket — and
shows in the chat tagged *via iMessage* / *via email*. Re-checking is safe: already-imported
messages are skipped, and anything that can't be matched is reported and left alone.

> Verified behavior, not yet run live here: the build environment has no Mac/Messages and no
> mail server, so the **matching, dedup, attaching, and the poll endpoint are all tested**,
> while the actual readers (`inbound.py`) run on your Mac first — they fail safely with a clear
> message if Full Disk Access or IMAP details are missing.

---

## PayPal invoicing

Send a customer a PayPal invoice straight from a repair, auto-filled from its issues,
services, and shipping — with a review-and-confirm step so nothing goes out by surprise.

**Setup** (Owners/Managers): create a REST app in the
[PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications), then in
**Settings → PayPal invoicing** paste the **Client ID** and **Secret**, choose
**Sandbox** (testing, fake money) or **Live**, and set your currency. The Secret is stored in
your local `repairdesk.db` and never leaves the Mac. **Start in sandbox** and send yourself a
test invoice before switching to live.

**Sending**: on a ticket, click **Create PayPal invoice**. RepairDesk fills the line items
from the repair (each issue/service at the high end of its estimate range, plus any shipping
cost) — edit descriptions, quantities, and amounts, add or remove lines, set the recipient
email and an optional note, and the running total updates live. Click **Send invoice** and
confirm: RepairDesk creates the invoice in PayPal and emails the customer a payment link. The
sent invoice (amount, recipient, a **View** link, and timestamp) is recorded on the ticket and
noted on its timeline.

> Verified behavior, not yet run live here: the build environment has no network or PayPal
> credentials, so the invoice **preview, editing, confirm step, recording, validation, and
> error handling are all tested**, but the actual API calls (`paypal.py`, written to PayPal's
> REST **Invoicing v2** API) are exercised by you first with **sandbox** credentials. If a send
> fails, PayPal's reason is shown and nothing is recorded.

---

---



Customers at `/request` can:

- **Add several devices in one request** — each device gets its own ticket number, but
  they only fill the form once.
- **Pick a device and see its issues** with prices (pulled from the Devices catalog), or
  choose *Other* to describe something not listed.
- **Choose how the device gets to you and back**, including any costs you've set.
- Watch a **running estimate** update as they tick issues and pick shipping.

Each device becomes its own *Pending review* ticket on your dashboard, tagged with the
chosen issues, shipping methods, and a rough quote you can adjust before confirming.

---

## Letting customers and your phone reach it

The web server listens on your whole network (`0.0.0.0:5050`), so other devices on the
**same Wi-Fi** (your phone, a customer's phone in the shop) can reach it.

1. Find your Mac's local IP: **System Settings → Wi-Fi → Details → IP Address**
   (looks like `192.168.1.20`).
2. In RepairDesk **Settings → Base URL**, set it to `http://192.168.1.20:5050`
   (use your actual address). This is what QR codes and tracking links point to.
3. Share these with customers:
   - Submit a repair: `http://192.168.1.20:5050/request`
   - Check status: `http://192.168.1.20:5050/track`

For access **outside** your shop network you'd need a tunnel (e.g. Tailscale or
Cloudflare Tunnel) or port forwarding — and you should add HTTPS first. The built-in
server is fine for a shop LAN; for public exposure, harden it.

---

## Importing your Google Sheet

1. In Google Sheets: **File → Download → Comma-separated values (.csv)**.
2. In RepairDesk: **Customers → Import**, choose the file, click **Import CSV**.

Columns named like *Name / First / Last / Phone / Email / Notes* are detected
automatically (a single *Name* column is split into first/last). Rows with a phone
that already exists are skipped. There's also a command-line version:

```bash
./.venv/bin/python import_csv.py ~/Downloads/customers.csv
```

---

## The QR / barcode workflow

1. Open a repair → **Print label** (or **Print sticker** on the right).
2. Stick the label on the device.
3. Later, scan the **QR** with your iPhone camera:
   - Signed in on that phone's browser → opens the full editable ticket.
   - Not signed in (a customer) → shows the public status page.
4. The **Code 128 barcode** encodes the repair number for a dedicated scanner gun.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | The whole web app (routes, database, auth) |
| `imessage.py` / `send_imessage.applescript` | iMessage sending via Messages |
| `mailer.py` | Email sending over SMTP (Gmail-compatible) |
| `paypal.py` | PayPal invoicing via the REST Invoicing v2 API (stdlib only) |
| `inbound.py` | Reads inbound replies — Messages `chat.db` (iMessage) + IMAP (email) |
| `labels.py` | QR + Code 128 barcode image generation |
| `import_csv.py` | Command-line CSV importer |
| `templates/`, `static/` | Web UI |
| `repairdesk.db` | Your data (created on first run — **this is your backup target**) |
| `secret.key` | Session signing key (created on first run; keep private) |

**Back up `repairdesk.db`** — that single file is all your customers and repairs.

---

## Notes & limits

- Staff sign in with their own **username + password** and have a **role** — Owner,
  Manager, or Technician — that controls what they can do (see *Staff accounts & roles*).
  For internet exposure, still add HTTPS in front (a tunnel or reverse proxy).
- Inbound replies aren't read (the app only sends). Reading replies is possible on
  macOS via the Messages database but needs Full Disk Access and is left out of this
  version.
- The SMTP password is stored in plain text inside `repairdesk.db` (a local file on your
  Mac). This is fine for a self-hosted shop app; use a Gmail **App Password** so it can be
  revoked independently of your account password.
