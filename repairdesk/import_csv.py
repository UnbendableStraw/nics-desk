#!/usr/bin/env python3
"""Import customers from a CSV (e.g. a Google Sheet export) on the command line.

Usage:
    python3 import_csv.py customers.csv

Recognized columns (case-insensitive): Name / First / Last / Phone / Email / Notes.
Duplicates (same phone) are skipped. The web UI (Customers → Import) does the same thing.
"""
import sys
import os

# Reuse the app's database + helpers so behavior matches the web importer.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as crm  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)

    crm.init_db()
    with crm.app.test_request_context():
        import csv
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print("That CSV has no header row.")
                sys.exit(1)
            norm = {h.lower().strip(): h for h in reader.fieldnames}

            def pick(row, *names):
                for n in names:
                    if n in norm:
                        return (row.get(norm[n]) or "").strip()
                return ""

            db = crm.get_db()
            existing = {r["phone"] for r in db.execute(
                "SELECT phone FROM customers WHERE phone != ''")}
            added = skipped = 0
            for row in reader:
                first = pick(row, "first name", "first", "firstname")
                last = pick(row, "last name", "last", "lastname", "surname")
                full = pick(row, "name", "full name", "customer", "customer name")
                if not (first or last) and full:
                    parts = full.split()
                    first, last = ((" ".join(parts[:-1]), parts[-1])
                                   if len(parts) > 1 else (full, ""))
                phone = crm.normalize_phone(pick(row, "phone", "mobile", "cell",
                                                 "phone number", "telephone"))
                email = pick(row, "email", "e-mail", "email address")
                notes = pick(row, "notes", "note", "device", "comments")
                if not (first or last or phone or email):
                    continue
                if phone and phone in existing:
                    skipped += 1
                    continue
                db.execute(
                    "INSERT INTO customers(first_name,last_name,phone,email,notes,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (first, last, phone, email, notes, crm.now_iso()))
                if phone:
                    existing.add(phone)
                added += 1
            db.commit()
            print(f"Imported {added} customer(s). Skipped {skipped} duplicate(s).")


if __name__ == "__main__":
    main()
