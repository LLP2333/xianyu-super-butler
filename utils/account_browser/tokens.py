"""Thread-safe, memory-only credentials observed from the user's native browser."""
from dataclasses import dataclass, field
from threading import RLock
import time


@dataclass(frozen=True)
class BrowserToken:
    account: str
    token: str = field(repr=False)
    device_id: str
    app_key: str
    cookies: str = field(repr=False)
    received_at: float = field(default_factory=time.time)


class TokenStore:
    def __init__(self):
        self._lock = RLock()
        self._candidates = {}
        self._active = {}
        self._approved = set()
        self._rejected = {}

    def observe(self, account_id, value):
        with self._lock:
            if value.token == self._rejected.get(account_id):
                return
            previous = self._candidates.get(account_id)
            # Seeing the same response twice must not extend its lifetime.
            if previous and previous.token == value.token:
                return
            self._candidates[account_id] = value
            if account_id in self._approved:
                self._active[account_id] = value

    def get(self, account_id, *, candidate=False, max_age=3600):
        with self._lock:
            value = (self._candidates if candidate else self._active).get(account_id)
            if value and 0 <= time.time() - value.received_at < max_age:
                return value
            return None

    def approve(self, account_id, value):
        with self._lock:
            if self.get(account_id, candidate=True) is not value:
                raise ValueError('网页 Token 已变化或过期，请重新确认')
            self._approved.add(account_id)
            self._active[account_id] = value

    def pause(self, account_id):
        with self._lock:
            self._approved.discard(account_id)

    def resume(self, account_id):
        with self._lock:
            if self.get(account_id):
                self._approved.add(account_id)

    def reject(self, account_id, token):
        with self._lock:
            self._rejected[account_id] = token
            for values in (self._active, self._candidates):
                if values.get(account_id) and values[account_id].token == token:
                    values.pop(account_id, None)

    def clear(self, account_id):
        with self._lock:
            self._candidates.pop(account_id, None)
            self._active.pop(account_id, None)
            self._approved.discard(account_id)
            self._rejected.pop(account_id, None)


tokens = TokenStore()
