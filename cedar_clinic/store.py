import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DOCTORS = [
    ("meera", "Dr. Meera Rao", "General medicine", "MR"),
    ("arjun", "Dr. Arjun Shah", "Cardiology", "AS"),
    ("nisha", "Dr. Nisha Patel", "Dermatology", "NP"),
]
TIMES = ("09:30", "11:00", "14:30", "16:00")


def now():
    return datetime.now(IST)


def normalize_phone(value):
    if not isinstance(value, str) or not re.fullmatch(r"\+?[0-9 ()-]+", value):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    # Treat +91 and national-format Indian numbers as the same demo identity.
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if 10 <= len(digits) <= 15 else None


class Store:
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY,
                    slot_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    cancelled INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_slot
                    ON appointments(slot_id) WHERE cancelled = 0;
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def slots(self):
        current = now()
        for offset in range(7):
            day = current.date() + timedelta(days=offset)
            for doctor_id, name, _, _ in DOCTORS:
                for time in TIMES:
                    start = datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=IST)
                    if start > current:
                        yield {
                            "id": f"{doctor_id}/{day}/{time}",
                            "doctor_id": doctor_id, "doctor": name,
                            "date": str(day), "time": time,
                        }

    def snapshot(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM appointments WHERE cancelled = 0 ORDER BY id DESC").fetchall()
        occupied = {row["slot_id"] for row in rows}
        doctors = []
        slots = list(self.slots())
        for doctor_id, name, specialty, initials in DOCTORS:
            doctors.append({
                "id": doctor_id, "name": name, "specialty": specialty, "initials": initials,
                "slots": [slot for slot in slots if slot["doctor_id"] == doctor_id and slot["id"] not in occupied],
            })
        return {"today": str(now().date()), "doctors": doctors, "appointments": [
            dict(self.details(row), phone="•••• " + row["phone"][-4:]) for row in rows
        ]}

    @staticmethod
    def details(row):
        doctor_id, date, time = row["slot_id"].split("/")
        name = next(name for key, name, _, _ in DOCTORS if key == doctor_id)
        return {"id": row["id"], "doctor": name, "date": date, "time": time}

    def availability(self, doctor_id=None, date=None):
        return [slot for doctor in self.snapshot()["doctors"] for slot in doctor["slots"]
                if (not doctor_id or doctor_id == doctor["id"]) and (not date or date == slot["date"])][:12]

    def book(self, slot_id, phone):
        if not any(slot["id"] == slot_id for slot in self.slots()):
            return {"ok": False, "message": "That slot is outside the current schedule."}
        try:
            with self.connection() as db:
                cursor = db.execute("INSERT INTO appointments(slot_id, phone) VALUES (?, ?)", (slot_id, phone))
                row = db.execute("SELECT * FROM appointments WHERE id = ?", (cursor.lastrowid,)).fetchone()
                return {"ok": True, "appointment": self.details(row)}
        except sqlite3.IntegrityError:
            return {"ok": False, "message": "That slot is already taken. Check availability again."}

    def cancel(self, phone, appointment_id=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM appointments
                   WHERE phone = ? AND cancelled = 0 AND (? IS NULL OR id = ?) ORDER BY slot_id""",
                (phone, appointment_id, appointment_id),
            ).fetchall()
            if not rows:
                return {"ok": False, "message": "No active appointment was found."}
            if len(rows) > 1:
                return {"ok": False, "appointments": [self.details(row) for row in rows],
                        "message": "Ask which appointment to cancel, then request confirmation again."}
            db.execute("UPDATE appointments SET cancelled = 1 WHERE id = ?", (rows[0]["id"],))
            return {"ok": True, "appointment": self.details(rows[0])}
