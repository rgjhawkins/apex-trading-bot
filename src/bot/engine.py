import threading
import time
from datetime import datetime


class BotEngine:
    def __init__(self, binance_client):
        self.client = binance_client
        self.running = False
        self.thread = None
        self.log = []
        self.stats = {
            "trades_today": 0,
            "pnl_today": 0.0,
            "started_at": None,
            "last_tick": None,
        }

    def start(self):
        if self.client is None:
            self._add_log("ERROR", "Cannot start: Binance client unavailable")
            return False
        if self.running:
            return False
        self.running = True
        self.stats["started_at"] = datetime.utcnow().isoformat()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self._add_log("INFO", "Bot engine started")
        return True

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self._add_log("INFO", "Bot engine stopped")
        self.stats["started_at"] = None
        return True

    def _loop(self):
        while self.running:
            try:
                self.stats["last_tick"] = datetime.utcnow().isoformat()
                # Strategy logic will be added here
            except Exception as e:
                self._add_log("ERROR", str(e))
            time.sleep(5)

    def _add_log(self, level: str, message: str):
        entry = {
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        self.log.insert(0, entry)
        if len(self.log) > 200:
            self.log = self.log[:200]

    def get_log(self, limit: int = 50) -> list:
        return self.log[:limit]

    def get_stats(self) -> dict:
        return {**self.stats, "running": self.running}
