"""Minimal client for the slskd REST API."""

import time
import urllib.parse

import requests


class Slskd:
    def __init__(self, url: str, api_key: str):
        self.base = f"{url}/api/v0"
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key

    def check(self) -> None:
        try:
            response = self.session.get(f"{self.base}/server", timeout=10)
        except requests.ConnectionError:
            raise SystemExit(f"Can't reach slskd at {self.base}. Is the container running?")
        if response.status_code == 401:
            raise SystemExit("slskd rejected the API key. Check SLSKD_API_KEY in .env and the key's cidr.")
        response.raise_for_status()
        if not response.json().get("isLoggedIn"):
            raise SystemExit("slskd isn't logged in to Soulseek. Check `docker logs slskd`.")

    def search(self, text: str, timeout_s: int) -> list[dict]:
        """Run a search and return its responses once it completes."""
        for attempt in range(5):
            response = self.session.post(f"{self.base}/searches", json={"searchText": text, "searchTimeout": timeout_s * 1000}, timeout=30)
            if response.status_code == 409:  # not logged in to the Soulseek server
                self.reconnect()
                continue
            if response.status_code == 429:  # only one search may start at a time
                time.sleep(1 + attempt)
                continue
            break
        response.raise_for_status()
        search_id = response.json()["id"]

        deadline = time.time() + timeout_s + 30
        while time.time() < deadline:
            time.sleep(1)
            if self.session.get(f"{self.base}/searches/{search_id}", timeout=30).json().get("isComplete"):
                break
        return self.session.get(f"{self.base}/searches/{search_id}/responses", timeout=30).json()

    def reconnect(self, wait_s: int = 120) -> None:
        """Wait for slskd to log back in to the Soulseek server, nudging it to reconnect.

        slskd can get stuck in a "Disconnecting" state after losing the server connection;
        asking it to connect restarts its reconnect loop and gets it out.
        """
        print("    slskd lost its Soulseek server connection; reconnecting...")
        deadline = time.time() + wait_s
        while time.time() < deadline:
            self.session.put(f"{self.base}/server", timeout=30)
            for _ in range(10):
                time.sleep(3)
                if self.session.get(f"{self.base}/server", timeout=30).json().get("isLoggedIn"):
                    print("    Reconnected.")
                    return
        raise RuntimeError(f"slskd couldn't log back in to the Soulseek server within {wait_s}s")

    def enqueue(self, batch_id: str, username: str, filename: str, size: int, destination: str) -> bool:
        body = {
            "id": batch_id,
            "username": username,
            "files": [{"filename": filename, "size": size}],
            "options": {"destination": destination},
        }
        for attempt in range(10):
            response = self.session.post(f"{self.base}/transfers/downloads/batches", json=body, timeout=60)
            if response.status_code == 429:  # only one enqueue may run at a time
                time.sleep(1 + attempt)
                continue
            return response.status_code == 201
        return False

    def downloads(self) -> list[dict]:
        """All tracked downloads, flattened out of the user/directory grouping."""
        users = self.session.get(f"{self.base}/transfers/downloads", timeout=30).json()
        return [file for user in users for directory in user["directories"] for file in directory["files"]]

    def cancel(self, username: str, transfer_id: str) -> None:
        self.session.delete(
            f"{self.base}/transfers/downloads/{urllib.parse.quote(username, safe='')}/{transfer_id}",
            params={"remove": "true"},
            timeout=30,
        )
