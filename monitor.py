"""
ClinicHQ Appointment Monitor - PAWS Grays Ferry
Clicks into Vaccine Clinic appointments and checks previous, current, and next week.
Emails you when anything new opens up, plus a daily summary at 8am.
"""

import subprocess
subprocess.run(["playwright", "install", "chromium", "--with-deps"], check=False)

import asyncio
import json
import os
import smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

CLINIC_URL = "https://app.clinichq.com/online/fe9babc7-f0d5-493b-8dfa-b2fda2f514d5"

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")

CHECK_INTERVAL_MINUTES = 20
DAILY_SUMMARY_HOUR     = 9  # 9am Eastern

STATE_FILE   = Path("last_slots.json")
SUMMARY_FILE = Path("daily_summary.json")

# ─────────────────────────────────────────────


def send_email(subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = GMAIL_ADDRESS

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print(f"[{datetime.now():%H:%M:%S}] Email sent: {subject}")


def load_summary() -> dict:
    if SUMMARY_FILE.exists():
        try:
            data = json.loads(SUMMARY_FILE.read_text())
            if data.get("date") == str(date.today()):
                return data
        except Exception:
            pass
    return {"date": str(date.today()), "checks": 0, "alerts_sent": 0}


def save_summary(data: dict):
    SUMMARY_FILE.write_text(json.dumps(data))


def maybe_send_daily_summary():
    now = datetime.now()
    summary = load_summary()

    # Only send if it's the 8am check and we haven't sent today yet
    if now.hour == DAILY_SUMMARY_HOUR and not summary.get("summary_sent"):
        checks    = summary.get("checks", 0)
        alerts    = summary.get("alerts_sent", 0)
        yesterday = summary.get("date", "today")

        if alerts > 0:
            status_line = f"ALERT: {alerts} new slot notification(s) were sent!"
        else:
            status_line = "No new appointments were found."

        body = (
            f"PAWS Vaccine Clinic Monitor — Daily Summary\n"
            f"{'=' * 45}\n\n"
            f"Date: {yesterday}\n"
            f"Checks run: {checks}\n"
            f"New slot alerts sent: {alerts}\n\n"
            f"Status: {status_line}\n\n"
            f"The monitor is running normally and checking every {CHECK_INTERVAL_MINUTES} minutes.\n"
            f"Booking page: {CLINIC_URL}"
        )

        send_email("PAWS Monitor Daily Summary", body)
        summary["summary_sent"] = True
        save_summary(summary)

        # Reset for next day
        SUMMARY_FILE.write_text(json.dumps({
            "date": str(date.today()),
            "checks": 0,
            "alerts_sent": 0,
            "summary_sent": False
        }))


async def get_page_text(page) -> str:
    await asyncio.sleep(3)
    return await page.inner_text("body")


async def click_button(page, name: str) -> bool:
    try:
        btn = page.get_by_role("button", name=name)
        await btn.wait_for(timeout=8000)
        await btn.click()
        await asyncio.sleep(3)
        return True
    except Exception:
        pass
    try:
        btn = page.locator(f"text={name}").first
        await btn.wait_for(timeout=5000)
        await btn.click()
        await asyncio.sleep(3)
        return True
    except Exception:
        return False


async def fetch_all_weeks() -> dict[str, str]:
    results = {"previous": "", "current": "", "next": ""}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await browser.new_page()

        print(f"[{datetime.now():%H:%M:%S}] Loading PAWS booking page...")
        await page.goto(CLINIC_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(4)

        print(f"[{datetime.now():%H:%M:%S}] Clicking Vaccine Clinic option...")
        clicked = await click_button(page, "Vaccine Clinic")
        if not clicked:
            try:
                await page.locator("text=Vaccine Clinic (shots) Appointment").click()
                await asyncio.sleep(3)
                clicked = True
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] Could not click Vaccine Clinic: {e}")
                await browser.close()
                return results

        print(f"[{datetime.now():%H:%M:%S}] On calendar page")

        results["current"] = await get_page_text(page)
        print(f"[{datetime.now():%H:%M:%S}] Captured current week")

        if await click_button(page, "Next"):
            results["next"] = await get_page_text(page)
            print(f"[{datetime.now():%H:%M:%S}] Captured next week")

            if await click_button(page, "Previous"):
                await asyncio.sleep(1)
                if await click_button(page, "Previous"):
                    results["previous"] = await get_page_text(page)
                    print(f"[{datetime.now():%H:%M:%S}] Captured previous week")
        else:
            print(f"[{datetime.now():%H:%M:%S}] Could not navigate weeks — only current week captured")

        await browser.close()

    return results


def extract_slots_with_claude(week_label: str, page_text: str) -> list[str]:
    if not page_text.strip():
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""You are reading the text of a veterinary clinic's online booking page ({week_label} week view).

Extract ALL available appointment slots — these are dates/times the user can actually click to book.
Ignore unavailable, greyed-out, or fully booked slots.

Return ONLY a JSON array of strings, one per available slot.
Format: "Day Month Date at Time" (e.g. "Tuesday April 29 at 2:00 PM")
If nothing is available, return: []
No explanation, no markdown — just the raw JSON array.

Page content:
{page_text[:8000]}"""
        }]
    )

    raw = response.content[0].text.strip()
    try:
        slots = json.loads(raw)
        return slots if isinstance(slots, list) else []
    except json.JSONDecodeError:
        print(f"[{datetime.now():%H:%M:%S}] Warning: could not parse Claude response for {week_label}: {raw[:200]}")
        return []


def load_last_slots() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_slots(slots: list[str]):
    STATE_FILE.write_text(json.dumps(slots))


async def check_once():
    print(f"\n[{datetime.now():%H:%M:%S}] Checking all three weeks...")

    summary = load_summary()
    summary["checks"] = summary.get("checks", 0) + 1
    save_summary(summary)

    try:
        weeks = await fetch_all_weeks()
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] Error: {e}")
        return

    all_slots = []
    for label, text in weeks.items():
        slots = extract_slots_with_claude(label, text)
        print(f"[{datetime.now():%H:%M:%S}] {label.capitalize()} week: {len(slots)} slot(s) — {slots}")
        all_slots.extend(slots)

    all_slots = list(set(all_slots))

    last_slots = load_last_slots()
    new_slots  = [s for s in all_slots if s not in last_slots]

    if new_slots:
        print(f"[{datetime.now():%H:%M:%S}] NEW slots detected: {new_slots}")
        slot_list = "\n".join(f"  - {s}" for s in new_slots)
        send_email(
            "New PAWS Vaccine Appointment Available!",
            f"New PAWS vaccine clinic appointment available!\n\n{slot_list}\n\nBook now: {CLINIC_URL}"
        )
        summary = load_summary()
        summary["alerts_sent"] = summary.get("alerts_sent", 0) + 1
        save_summary(summary)
    else:
        print(f"[{datetime.now():%H:%M:%S}] No new slots across any week.")

    save_slots(all_slots)
    maybe_send_daily_summary()


async def main():
    print("=" * 50)
    print("PAWS Vaccine Clinic Appointment Monitor")
    print(f"Checking every {CHECK_INTERVAL_MINUTES} minutes")
    print(f"Daily summary at {DAILY_SUMMARY_HOUR}:00am")
    print("Monitoring: previous, current, and next week")
    print("=" * 50)

    while True:
        await check_once()
        print(f"[{datetime.now():%H:%M:%S}] Next check in {CHECK_INTERVAL_MINUTES} minutes...")
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
