import re

from .store import normalize_phone

DIGITS = dict(zip("zero one two three four five six seven eight nine".split(), "0123456789"))
DIGITS["oh"] = "0"
AFFIRMATIONS = {"yes", "yeah", "yep", "correct", "that is correct", "that's correct",
                "yes please", "yes that's correct", "yes that is correct", "confirm", "yes confirm"}


class Confirmation:
    def __init__(self):
        self.turn = 0
        self.last = ""
        self.numbers = set()
        self.latest_phone = None
        self.pending = None

    def hear(self, text):
        self.turn += 1
        self.last = " ".join(re.findall(r"[a-z']+", text.lower().replace("’", "'"))) if not re.search(r"[0-9]", text) else ""
        if self.pending and (self.last not in AFFIRMATIONS or self.turn > self.pending[1] + 1):
            self.pending = None
        # STT may transcribe a phone number as digits, words, or both.
        parts = re.findall(r"\d+|[a-z]+|[.!?;]", text.lower()) + ["."]
        digits = ""
        for part in parts:
            if part.isascii() and part.isdigit():
                digits += part
            elif part in DIGITS:
                digits += DIGITS[part]
            else:
                phone = normalize_phone(digits)
                if phone:
                    self.numbers.add(phone)
                    self.latest_phone = phone
                digits = ""

    def request(self, action, phone, target):
        key = (action, phone, target)
        if phone not in self.numbers:
            self.pending = None
            return False, {"status": "need_phone", "message": "Ask the caller to say the full phone number digit by digit."}
        if self.pending == (key, self.turn - 1) and self.last in AFFIRMATIONS:
            self.pending = None
            return True, None
        self.pending = (key, self.turn)
        readback = ", ".join(next(word for word, digit in DIGITS.items() if digit == char) for char in phone)
        return False, {"status": "needs_confirmation", "readback": readback}
