"""Spotify Web API client: reads playlists and albums via the Authorization Code + PKCE flow."""

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

from .log import log

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
# Must be registered as a Redirect URI in the Spotify developer dashboard
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative"


@dataclass
class Track:
    id: str
    title: str
    artists: list[str]
    album: str
    duration_ms: int

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    def __str__(self) -> str:
        return f"{self.artist} - {self.title}"


def parse_link(link: str) -> tuple[str, str]:
    """Split a Spotify link or URI into its kind ("playlist" or "album") and ID. A bare ID is taken as a playlist."""
    match = re.search(r"(playlist|album)[/:]([A-Za-z0-9]{22})", link)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(r"[A-Za-z0-9]{22}", link.strip()):
        return "playlist", link.strip()
    raise SystemExit(f"Not a Spotify playlist or album link: {link}")


class Spotify:
    def __init__(self, client_id: str, token_path: Path):
        self.client_id = client_id
        self.token_path = token_path
        self.token = json.loads(token_path.read_text()) if token_path.exists() else None

    def fetch(self, link: str) -> tuple[str, list[Track]]:
        """The name and tracks of the playlist or album a link points to."""
        kind, item_id = parse_link(link)
        return self.album(item_id) if kind == "album" else self.playlist(item_id)

    def album(self, album_id: str) -> tuple[str, list[Track]]:
        album = self._get(f"{API_URL}/albums/{album_id}")
        name = f"{', '.join(a['name'] for a in album['artists'])} - {album['name']}"
        tracks = []
        page = album["tracks"]
        while page:
            for item in page["items"]:
                if item.get("type") != "track" or not item.get("id"):
                    continue
                tracks.append(Track(
                    id=item["id"],
                    title=item["name"],
                    artists=[a["name"] for a in item["artists"]],
                    album=album["name"],
                    duration_ms=item["duration_ms"],
                ))
            page = self._get(page["next"]) if page.get("next") else None
        return name, tracks

    def playlist(self, playlist_id: str) -> tuple[str, list[Track]]:
        name = self._get(f"{API_URL}/playlists/{playlist_id}", {"fields": "name"})["name"]
        tracks = []
        url, params = f"{API_URL}/playlists/{playlist_id}/items", {"limit": 50, "additional_types": "track"}
        while url:
            page = self._get(url, params)
            for entry in page["items"]:
                item = entry.get("item")
                if entry.get("is_local") or not item or item.get("type") != "track" or not item.get("id"):
                    continue
                tracks.append(Track(
                    id=item["id"],
                    title=item["name"],
                    artists=[a["name"] for a in item["artists"]],
                    album=item["album"]["name"],
                    duration_ms=item["duration_ms"],
                ))
            url, params = page.get("next"), None
        return name, tracks

    def _get(self, url: str, params: dict | None = None) -> dict:
        for _ in range(5):
            response = requests.get(url, params=params, headers={"Authorization": f"Bearer {self._access_token()}"}, timeout=30)
            if response.status_code == 401:
                self.token["expires_at"] = 0
                continue
            if response.status_code == 429:
                time.sleep(int(response.headers.get("Retry-After", 5)))
                continue
            if response.status_code == 403 and "/playlists/" in url:
                raise SystemExit(
                    "Spotify refused access to this playlist. Apps in Development Mode can only read playlists you own "
                    "or collaborate on. In Spotify, add its tracks to a playlist of your own and use that link instead."
                )
            response.raise_for_status()
            return response.json()
        raise SystemExit(f"Spotify request kept failing: {url}")

    def _access_token(self) -> str:
        if not self.token:
            self._authorize()
        elif time.time() > self.token["expires_at"] - 60:
            self._refresh()
        return self.token["access_token"]

    def _authorize(self) -> None:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        })
        log.info(f"Log in to Spotify (one time only) by opening this link in your browser:\n{url}\n")
        query = _wait_for_callback()
        if query.get("state") != state or "code" not in query:
            raise SystemExit(f"Spotify login failed: {query.get('error', 'unexpected response')}")
        self._request_token({
            "grant_type": "authorization_code",
            "code": query["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        })

    def _refresh(self) -> None:
        try:
            self._request_token({"grant_type": "refresh_token", "refresh_token": self.token["refresh_token"]})
        except requests.HTTPError:
            self.token = None
            self._authorize()

    def _request_token(self, data: dict) -> None:
        response = requests.post(TOKEN_URL, data={**data, "client_id": self.client_id}, timeout=30)
        response.raise_for_status()
        token = response.json()
        # refresh responses may omit the refresh token, meaning the old one stays valid
        token.setdefault("refresh_token", (self.token or {}).get("refresh_token"))
        token["expires_at"] = time.time() + token["expires_in"]
        self.token = token
        self.token_path.write_text(json.dumps(token))
        self.token_path.chmod(0o600)


def _wait_for_callback() -> dict:
    """Serve one request on the redirect URI and return its query parameters."""
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            result.update(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Logged in. You can close this tab.")

        def log_message(self, *args):
            pass

    # Listen on every interface: inside the container, 127.0.0.1 only reaches the container itself.
    # docker-compose.yml publishes the port on the host's 127.0.0.1 only.
    with HTTPServer(("0.0.0.0", urllib.parse.urlparse(REDIRECT_URI).port), Handler) as server:
        while not result:
            server.handle_request()
    return result
