# render_api.py
"""
Thin Render.com API wrapper using requests.

Endpoints implemented (common ones used by the bot):
- GET /services
- GET /services/{id}
- POST /services/{id}/restart
- DELETE /services/{id}
- POST /services/{id}/deploys
- GET /services/{id}/deploys
- GET /services/{id}/logs?tail=...
- GET /services/{id}/env-vars
- POST /services/{id}/env-vars  (upsert)
- DELETE /services/{id}/env-vars/{KEY}
- PATCH /services/{id} (for repo/branch updates)
- GET /owners/own-current (owner info) with fallbacks
"""

import requests
from typing import Any, Dict, Optional, Tuple

BASE_URL = "https://api.render.com/v1"


class RenderAPI:
    def __init__(self, api_key: str, timeout: int = 25):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, path: str, params: Optional[Dict] = None, json_data: Optional[Dict] = None
    ) -> Tuple[bool, Any]:
        url = BASE_URL + path
        try:
            r = requests.request(
                method, url, headers=self.headers, params=params, json=json_data, timeout=self.timeout
            )
        except Exception as e:
            return False, f"Request error: {e}"

        try:
            body = r.json()
        except Exception:
            body = r.text or ""

        if 200 <= r.status_code < 300:
            return True, body
        else:
            # Return the parsed error if present
            return False, body

    # --- Basic / tests ---
    def test_key(self) -> Tuple[bool, Any]:
        return self._request("GET", "/services", params={"limit": 1})

    def owner(self) -> Tuple[bool, Any]:
        # Try common owner endpoints with fallbacks
        ok, data = self._request("GET", "/owners/own-current")
        if ok:
            return True, data
        ok, data = self._request("GET", "/owners")
        if ok and isinstance(data, list) and data:
            return True, data[0]
        ok, data = self._request("GET", "/accounts")
        if ok and isinstance(data, list) and data:
            return True, data[0]
        return False, data

    # --- Services ---
    def list_services(self) -> Tuple[bool, Any]:
        return self._request("GET", "/services")

    def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}")

    def delete_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/services/{service_id}")

    def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("POST", f"/services/{service_id}/restart")

    # --- Deploys ---
    def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        return self._request("POST", f"/services/{service_id}/deploys", json_data={"clearCache": clear_cache})

    def list_deploys(self, service_id: str, limit: int = 10) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/deploys", params={"limit": limit})

    # --- Logs ---
    def get_logs(self, service_id: str, tail: int = 200) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/logs", params={"tail": tail})

    # --- Env vars ---
    def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/env-vars")

    def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        payload = {"envVars": [{"key": k, "value": v} for k, v in kv.items()]}
        return self._request("POST", f"/services/{service_id}/env-vars", json_data=payload)

    def delete_env_var(self, service_id: str, key: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/services/{service_id}/env-vars/{key}")

    # --- GitHub / repo updates ---
    def set_repo(self, service_id: str, repo: str, branch: str = "main", build_command: Optional[str] = None) -> Tuple[bool, Any]:
        payload: Dict[str, Any] = {"repo": repo, "branch": branch}
        if build_command:
            payload["buildCommand"] = build_command
        # PATCH to update the service with repo/branch
        return self._request("PATCH", f"/services/{service_id}", json_data=payload)
