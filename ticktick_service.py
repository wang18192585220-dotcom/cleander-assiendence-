#!/usr/bin/env python3
"""
TickTick API Service — OAuth token management + Task CRUD.

Architecture:
  TickTickTokenManager → stores/retrieves tokens in SQLite
  TickTickService     → wraps TickTick Open API (v1)

Security:
  All TickTick secrets (client_id, client_secret, tokens) stay server-side only.
  Frontend never sees them — it only calls backend API endpoints.
"""

import json
import os
import sqlite3
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

# ── Constants ──────────────────────────────────────────────────────────────
TICKTICK_API_BASE = "https://api.ticktick.com/open/v1"
TICKTICK_AUTH_URL = "https://ticktick.com/oauth/authorize"
TICKTICK_TOKEN_URL = "https://ticktick.com/oauth/token"

# ── Config from environment / .env ─────────────────────────────────────────
TICKTICK_CLIENT_ID = os.environ.get("TICKTICK_CLIENT_ID", "")
TICKTICK_CLIENT_SECRET = os.environ.get("TICKTICK_CLIENT_SECRET", "")
TICKTICK_REDIRECT_URI = os.environ.get("TICKTICK_REDIRECT_URI", "http://localhost:8765/api/ticktick/callback")

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes/profiles/energy-management"))
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PROJECT_DIR, "task_history.db")

# Also try reading from .env files
def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "TICKTICK_CLIENT_ID" and not TICKTICK_CLIENT_ID:
                globals()["TICKTICK_CLIENT_ID"] = val
            elif key == "TICKTICK_CLIENT_SECRET" and not TICKTICK_CLIENT_SECRET:
                globals()["TICKTICK_CLIENT_SECRET"] = val
            elif key == "TICKTICK_REDIRECT_URI":
                globals()["TICKTICK_REDIRECT_URI"] = val

_load_env(os.path.join(PROJECT_DIR, ".env"))
_load_env(os.path.join(HERMES_HOME, ".env"))


# ═══════════════════════════════════════════════════════════════════════════
# TickTickTokenManager
# ═══════════════════════════════════════════════════════════════════════════

class TickTickTokenManager:
    """Manages TickTick OAuth tokens in SQLite (local only, never exposed)."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._ensure_table()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticktick_tokens (
                user_id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_expires_at TEXT,
                ticktick_user_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def get_stored_token(self, user_id="default"):
        """Return token dict or None."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM ticktick_tokens WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {
            "access_token": row["access_token"],
            "refresh_token": row["refresh_token"],
            "expires_at": row["token_expires_at"],
            "ticktick_user_id": row["ticktick_user_id"],
        }

    def save_tokens(self, access_token, refresh_token, expires_in=0,
                    ticktick_user_id="", user_id="default"):
        """Save (or update) tokens for a user."""
        now = datetime.now().isoformat()
        expires_at = None
        if expires_in > 0:
            expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

        conn = self._conn()
        conn.execute("""
            INSERT INTO ticktick_tokens
                (user_id, access_token, refresh_token, token_expires_at,
                 ticktick_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_expires_at = excluded.token_expires_at,
                ticktick_user_id = excluded.ticktick_user_id,
                updated_at = excluded.updated_at
        """, (user_id, access_token, refresh_token, expires_at,
              ticktick_user_id, now, now))
        conn.commit()
        conn.close()

    def is_connected(self, user_id="default"):
        """Check if user has valid (non-expired) tokens."""
        token = self.get_stored_token(user_id)
        if not token or not token["access_token"]:
            return False
        if token["expires_at"]:
            try:
                exp = datetime.fromisoformat(token["expires_at"])
                if exp <= datetime.now():
                    return False
            except (ValueError, TypeError):
                pass
        return True


# ═══════════════════════════════════════════════════════════════════════════
# TickTickService
# ═══════════════════════════════════════════════════════════════════════════

class TickTickService:
    """Wraps TickTick Open API (v1) for task CRUD."""

    def __init__(self, db_path=DB_PATH):
        self.token_mgr = TickTickTokenManager(db_path)
        self._state_store = {}  # {state: timestamp} for CSRF

    # ── OAuth helpers ──────────────────────────────────────────────────

    def get_auth_url(self, state=None):
        """Generate TickTick OAuth authorization URL."""
        if state is None:
            state = uuid.uuid4().hex
        self._state_store[state] = time.time()
        # Clean expired states (>10 min)
        cutoff = time.time() - 600
        self._state_store = {k: v for k, v in self._state_store.items() if v > cutoff}

        params = {
            "client_id": TICKTICK_CLIENT_ID,
            "scope": "tasks:read tasks:write",
            "state": state,
            "redirect_uri": TICKTICK_REDIRECT_URI,
            "response_type": "code",
        }
        return TICKTICK_AUTH_URL + "?" + urllib.parse.urlencode(params)

    def verify_state(self, state):
        """Verify OAuth state parameter (CSRF protection)."""
        return state in self._state_store

    def exchange_code(self, code, user_id="default"):
        """Exchange OAuth authorization code for access + refresh tokens."""
        if not TICKTICK_CLIENT_ID or not TICKTICK_CLIENT_SECRET:
            raise ValueError("TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET must be set")

        body = urllib.parse.urlencode({
            "client_id": TICKTICK_CLIENT_ID,
            "client_secret": TICKTICK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TICKTICK_REDIRECT_URI,
        }).encode()

        req = urllib.request.Request(
            TICKTICK_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"Token exchange failed ({e.code}): {err_body}")
        except Exception as e:
            raise RuntimeError(f"Token exchange failed: {e}")

        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token", "")
        expires_in = data.get("expires_in", 0)
        ticktick_user_id = data.get("user_id", "") or data.get("open_id", "")

        if not access_token:
            raise RuntimeError(f"No access_token in response: {json.dumps(data)}")

        self.token_mgr.save_tokens(
            access_token, refresh_token, expires_in,
            ticktick_user_id=ticktick_user_id, user_id=user_id,
        )
        return data

    def refresh_token(self, user_id="default"):
        """Use refresh_token to get a new access_token."""
        stored = self.token_mgr.get_stored_token(user_id)
        if not stored or not stored["refresh_token"]:
            raise RuntimeError("No refresh token available — please re-authorize")

        body = urllib.parse.urlencode({
            "client_id": TICKTICK_CLIENT_ID,
            "client_secret": TICKTICK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
        }).encode()

        req = urllib.request.Request(
            TICKTICK_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"Token refresh failed ({e.code}): {err_body}")
        except Exception as e:
            raise RuntimeError(f"Token refresh failed: {e}")

        access_token = data.get("access_token", "")
        refresh_token_new = data.get("refresh_token", stored["refresh_token"])
        expires_in = data.get("expires_in", 0)

        self.token_mgr.save_tokens(
            access_token, refresh_token_new, expires_in,
            ticktick_user_id=stored.get("ticktick_user_id", ""),
            user_id=user_id,
        )
        return data

    # ── HTTP helpers ───────────────────────────────────────────────────

    def _get_headers(self, user_id="default"):
        """Get authorization headers with valid access token (auto-refresh)."""
        stored = self.token_mgr.get_stored_token(user_id)
        if not stored or not stored["access_token"]:
            raise RuntimeError("Not connected to TickTick — please authorize first")

        # Check if token is expired
        if stored["expires_at"]:
            try:
                exp = datetime.fromisoformat(stored["expires_at"])
                if exp <= datetime.now():
                    self.refresh_token(user_id)
                    stored = self.token_mgr.get_stored_token(user_id)
            except (ValueError, TypeError):
                pass

        return {
            "Authorization": f"Bearer {stored['access_token']}",
            "Content-Type": "application/json",
        }

    def _api_request(self, method, path, body=None, user_id="default"):
        """Make an API request to TickTick. Auto-refreshes token on 401."""
        url = TICKTICK_API_BASE + path
        headers = self._get_headers(user_id)

        data = None
        if body is not None:
            data = json.dumps(body).encode()

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            resp = urllib.request.urlopen(req, timeout=20)
            raw = resp.read().decode()
            if not raw.strip():
                return {}
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Try refreshing token once
                try:
                    self.refresh_token(user_id)
                    headers = self._get_headers(user_id)
                    req = urllib.request.Request(url, data=data, headers=headers, method=method)
                    resp = urllib.request.urlopen(req, timeout=20)
                    raw = resp.read().decode()
                    return json.loads(raw) if raw.strip() else {}
                except Exception as e2:
                    raise RuntimeError(f"API request failed after token refresh: {e2}")
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"TickTick API error ({e.code}): {err_body[:500]}")

    # ── Public API methods ─────────────────────────────────────────────

    def get_user_info(self, user_id="default"):
        """Get authenticated user info."""
        try:
            return self._api_request("GET", "/user", user_id=user_id)
        except Exception:
            return {}

    def get_projects(self, user_id="default"):
        """List all TickTick projects/lists."""
        return self._api_request("GET", "/project", user_id=user_id)

    def get_project_by_name(self, name, user_id="default"):
        """Find a project by name. Returns project dict or None."""
        projects = self.get_projects(user_id)
        if isinstance(projects, list):
            for p in projects:
                if p.get("name", "").lower() == name.lower():
                    return p
        return None

    def create_task(self, title, content="", project_id=None,
                    due_date=None, priority=0, user_id="default"):
        """Create a task in TickTick. Returns task dict."""
        body = {
            "title": title,
            "priority": priority,
        }
        if content:
            body["content"] = content
        if project_id:
            body["projectId"] = project_id
        if due_date:
            body["dueDate"] = due_date

        return self._api_request("POST", "/task", body=body, user_id=user_id)

    def update_task(self, project_id, task_id, title=None, content=None,
                    due_date=None, priority=None, user_id="default"):
        """Update a task in TickTick. Returns task dict."""
        body = {}
        if title is not None:
            body["title"] = title
        if content is not None:
            body["content"] = content
        if due_date is not None:
            body["dueDate"] = due_date
        if priority is not None:
            body["priority"] = priority

        if not body:
            return {}

        return self._api_request(
            "PATCH", f"/task/{project_id}/{task_id}",
            body=body, user_id=user_id,
        )

    def complete_task(self, project_id, task_id, user_id="default"):
        """Mark a task as completed in TickTick."""
        return self._api_request(
            "POST", f"/project/{project_id}/task/{task_id}/complete",
            body={}, user_id=user_id,
        )

    def delete_task(self, project_id, task_id, user_id="default"):
        """Delete a task from TickTick. Returns {} on success."""
        return self._api_request(
            "DELETE", f"/project/{project_id}/task/{task_id}",
            user_id=user_id,
        )

    # ── Convenience ────────────────────────────────────────────────────

    def is_connected(self, user_id="default"):
        return self.token_mgr.is_connected(user_id)

    def get_status(self, user_id="default"):
        """Get connection status for /api/ticktick/status."""
        connected = self.is_connected(user_id)
        account = None
        if connected:
            info = self.get_user_info(user_id)
            account = info.get("name") or info.get("email") or info.get("username", "")
        return {"connected": connected, "account": account or None}


# ── Module-level test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== TickTick Service Test ===")
    mgr = TickTickTokenManager(DB_PATH)
    print(f"TokenManager OK (db: {DB_PATH})")
    print(f"Connected: {mgr.is_connected()}")

    svc = TickTickService(DB_PATH)
    print(f"Service OK")

    if svc.is_connected():
        print("Status:", json.dumps(svc.get_status(), indent=2))
        try:
            projects = svc.get_projects()
            print(f"Projects: {len(projects) if isinstance(projects, list) else 'N/A'}")
        except Exception as e:
            print(f"Projects error: {e}")
    else:
        print("Not connected to TickTick yet.")
        if TICKTICK_CLIENT_ID:
            print("Auth URL:", svc.get_auth_url()[:120] + "...")
        else:
            print("TICKTICK_CLIENT_ID not configured — set in .env")
