# RepairDesk — Live-Run Checklist

Everything in RepairDesk is tested except the bits that *must* touch your Mac or the
network: sending iMessages and email, the customer magic-link, PayPal, and reading replies
back in. This checklist walks you through turning each one on and smoke-testing it, **in
order** — later steps depend on earlier ones (email before the portal; sandbox before live
PayPal).

Work top to bottom. Each step says what it unlocks, how to set it up, how to test it, what
"working" looks like, and what to do if it doesn't. Tick the boxes as you go.

> Everything stays on this Mac. Credentials live in `repairdesk.db` next to the app; nothing
> is sent anywhere except the services you explicitly configure.

---

## 0. First launch & basics  *(do this once)*

- [ ] In Terminal, `cd` into the `repairdesk` folder and run `./run.sh`. First time, it
      creates a virtual environment and installs dependencies (Flask, qrcode, python-barcode,
      Pillow). You need Python 3 (`brew install python` if missing).
- [ ] You should see `RepairDesk is running.` with an `Admin:` URL. Open
      **http://localhost:5050** in your browser.
- [ ] Sign in as username **`owner`** / password **`changeme`** (or your existing password if
      upgrading).
- [ ] **Settings → Staff password**: change it immediately.
- [ ] **Settings → Shop**: set your **Shop name** and confirm **Base URL**. This matters a lot:
      every customer link (status pages, QR codes, magic-link, track) is built from Base URL.
      - Testing on the Mac only? `http://localhost:5050` is fine.
      - Customers on your shop Wi-Fi? Use the Mac's LAN address, e.g. `http://192.168.1.50:5050`
        (the app prints a `Customers:` URL at startup as a hint).
      - Exposed over the internet? Use your public HTTPS hostname (see step 8).
- [ ] **Users**: add accounts for staff with the right role (Owner / Manager / Technician).

**Keep the Terminal window open** — it's where dev/fallback messages print (handy below).

---

## 1. iMessage sending  *(Mac only)*

**Unlocks:** one-click status texts to customers from a ticket.

- [ ] Open the **Messages** app and sign in with your Apple ID. Send yourself a test iMessage
      to confirm Messages itself works.
- [ ] Make sure the app isn't in dev mode: at startup the Terminal should **not** say
      *"not on macOS — iMessage sending is in dev/log mode."* (If it does, you're not running
      on the Mac.)
- [ ] **Test:** open a real ticket whose customer has a phone number → **Send a message** →
      channel **iMessage** → type something → **Send**.
- [ ] **Working looks like:** a green "iMessage sent" banner, the text arrives in Messages, and
      the send shows on the ticket's **Activity**.

**If it fails:**
- *"macOS blocked automation…"* (error -1743): **System Settings → Privacy & Security →
  Automation** → allow your Terminal (and/or Python) to control **Messages**. Re-run `./run.sh`.
- *"No phone number on file"*: add a phone to the customer.
- First send may pop a one-time macOS permission prompt — click **OK**, then resend.

---

## 2. Email sending (SMTP)  *(network)*

**Unlocks:** status emails, and it's a prerequisite for the customer portal (step 3).

Gmail is the easy path. Use a professional Gmail/Workspace account.

- [ ] In Google: enable **2-Step Verification**, then create an **App Password**
      (Google Account → Security → App passwords). It's a 16-character code — **not** your normal
      password.
- [ ] **Settings → Email**: tick **Enable email**, then set
      - Host `smtp.gmail.com`, Port `587`, Security `STARTTLS`
      - User = your full Gmail address
      - Password = the App Password
      - From = the address customers should see (usually the same Gmail).
- [ ] Save. (Other providers: use their SMTP host/port; STARTTLS on 587 or SSL on 465.)
- [ ] **Test:** on a ticket whose customer has an email → **Send a message** → channel
      **Email** → **Send**.
- [ ] **Working looks like:** an "Email sent" banner and the message lands in the customer's
      inbox (check spam the first time).

**If it fails:**
- *Authentication errors*: you're almost certainly using your normal password — switch to an
  **App Password**. Confirm 2-Step Verification is on.
- *Connection/timeout*: re-check host/port/security; some networks block port 587.
- The password is stored locally; leaving the password field **blank** on a later save keeps
  the saved one.

---

## 3. Customer portal (email magic-link)  *(needs step 2)*

**Unlocks:** returning customers sign in to see all their repairs, start warranties, and chat.

- [ ] Confirm **email sending works** (step 2) — the portal can't send links without it.
- [ ] **Settings → Customer portal**: tick **Enable the customer portal**. Adjust the sign-in
      email if you like (keep the `{link}` placeholder).
- [ ] Make sure **Base URL** (step 0) is the address customers actually use, or the link in the
      email will point somewhere they can't reach.
- [ ] **Test:** create a test customer with **your own email**, then open
      `BASE_URL/portal`, enter that email, and request a link.
- [ ] **Working looks like:** you receive the email, the link signs you in, and you see that
      customer's repairs.

**Handy during setup:** the sign-in link is also printed in the Terminal as
`[PORTAL LINK] for <email>: <url>` — so even before email is perfect, you can copy it from
there to test the rest of the flow. The link is single-use and expires in 30 minutes.

**If it fails:**
- *"Email sign-in isn't available"*: email (SMTP) isn't configured — finish step 2.
- *Link 404s or goes nowhere*: Base URL is wrong — set it to the URL you're actually opening.

---

## 4. PayPal invoicing  *(network + credentials — sandbox first!)*

**Unlocks:** send a customer a PayPal invoice auto-filled from a repair.

- [ ] At **developer.paypal.com → Apps & Credentials**, create a **REST app**. Start on the
      **Sandbox** tab and copy its **Client ID** and **Secret**.
- [ ] **Settings → PayPal invoicing**: paste Client ID + Secret, set **Environment = Sandbox**,
      and your **Currency**. Save.
- [ ] In the PayPal sandbox, note a **personal test account** email (Developer Dashboard →
      Sandbox → Accounts) — that's your pretend customer.
- [ ] **Test:** open a ticket (ideally one with a few issues/services so lines auto-fill) →
      **Create PayPal invoice** → review/edit the lines → set the recipient to the sandbox
      personal account → **Send invoice** → confirm.
- [ ] **Working looks like:** a success banner, the invoice recorded on the ticket with a
      **View** link, a timeline note, and the invoice visible in your sandbox PayPal account.
- [ ] **Go live only when sandbox works:** create a **Live** REST app, paste its Live
      credentials, switch **Environment = Live**, and send one real invoice to yourself first.

**If it fails:** the exact PayPal reason is shown in the red banner and **nothing is recorded**.
Most common: credentials don't match the selected Environment (live keys in sandbox mode or
vice-versa). Blank Secret on a later save keeps the saved one.

---

## 5. Reading inbound iMessage replies  *(Mac only — needs Full Disk Access)*

**Unlocks:** customer iMessage replies land in the matching ticket's chat.

- [ ] **Grant Full Disk Access** to whatever runs the app, so it can read
      `~/Library/Messages/chat.db`:
      **System Settings → Privacy & Security → Full Disk Access** → add your **Terminal**
      (simplest), or the Python binary at `repairdesk/.venv/bin/python3`. Quit and re-run
      `./run.sh` afterward.
- [ ] **Settings → Reading customer replies**: tick **Read inbound iMessages from this Mac**.
      Save.
- [ ] **Test:** from a phone that matches a customer's number on file, text the shop's iMessage
      number a reply (bonus: include a repair number like `PH-1001`). Then on the **Repairs**
      page click **Check replies**.
- [ ] **Working looks like:** the banner reports *"attached 1"*, and the message shows in that
      ticket's **Customer chat** tagged *via iMessage* (and in the customer's portal).

**How matching works:** sender phone → customer (last-10-digits match, so `+1`/formatting is
fine); a repair number in the text picks the exact ticket, otherwise their most recent one.
Re-checking is safe — already-imported messages are skipped.

**If it fails:**
- *"…grant this app Full Disk Access"*: the permission isn't applied to the process actually
  running — add Terminal (or the venv python), then fully quit and relaunch.
- *"attached 0 … unmatched"*: the sender's number doesn't match any customer's phone, or they
  have no tickets yet.

---

## 6. Reading inbound email replies (IMAP)  *(network)*

**Unlocks:** customer email replies land in the matching ticket's chat.

- [ ] **Settings → Reading customer replies**: tick **Read inbound email over IMAP** and fill in
      - Host `imap.gmail.com`, Port `993`
      - User = the mailbox address, Password = an **App Password** (same kind as step 2)
      - Mailbox `INBOX`. Save.
- [ ] **Test:** from an email address on a customer's record, send the shop mailbox a message
      (put a repair number in the subject to target a specific ticket). Then **Check replies**.
- [ ] **Working looks like:** the banner reports it attached the message, and it appears in the
      ticket chat tagged *via email*. Imported messages are **marked read** in the mailbox, so
      they won't be re-imported.

**If it fails:**
- *"Couldn't read email over IMAP…"*: check host/port, and that you used an **App Password**.
- *Nothing attached*: the sender address isn't on any customer record, or the mail was already
  marked read.

---

## 7. (Optional) Checking replies on a schedule

The supported way to pull replies is the **Check replies** button on the Repairs page (it pulls
both iMessage and email at once). A quick low-tech habit: keep the Repairs page open and click
it through the day.

> **Heads-up / known limitation:** the `/inbound/poll` endpoint requires a signed-in staff
> session, so a headless `cron`/`launchd` job using `curl` would just bounce to the login page —
> it won't work as-is. Automated background polling needs an auth-token mechanism that isn't
> built yet (a good candidate for a future enhancement). For now, use the button.

---

## 8. (Optional) Letting customers reach it safely

`localhost:5050` and the LAN address only work on your network. To let customers use the portal
and links from anywhere **without opening ports on your router**, put a tunnel in front (the
README's recommended setup): a **Cloudflare Tunnel** or **Tailscale Funnel** gives you a public
HTTPS hostname that forwards to the Mac.

- [ ] Stand up the tunnel to `http://localhost:5050`.
- [ ] Set **Settings → Base URL** to that public `https://…` hostname so every link customers
      get points to it.
- [ ] Consider putting **Cloudflare Access** (or similar) in front of the staff/admin routes,
      leaving `/track`, `/request`, and `/portal` public.

---

## Quick troubleshooting reference

| Symptom | Likely cause / fix |
|---|---|
| iMessage send fails, mentions automation / `-1743` | Allow Terminal→Messages under Privacy & Security → **Automation**, relaunch |
| Email won't send / auth error | Use a Gmail **App Password**, not your normal password; 2-Step on |
| "Email sign-in isn't available" on the portal | Configure email (SMTP) first |
| Magic-link / QR / track link points nowhere | **Base URL** is wrong — set it to the URL customers actually use |
| PayPal send fails | Read the red banner's reason; usually keys don't match the chosen Environment |
| "…grant Full Disk Access" when checking replies | Add Terminal / venv python to **Full Disk Access**, quit & relaunch |
| Reply imported but on the wrong ticket | Add the repair number to the message; otherwise it attaches to the most recent ticket |
| Replies not importing | Sender's phone/email isn't on a customer record; or (email) already marked read |

When something's off, the **Terminal window** running `./run.sh` is your best friend — dev-mode
notes and fallback links print there.
