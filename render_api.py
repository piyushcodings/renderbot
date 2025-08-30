# render_api.py
"""
Render API small wrapper tailored for the bot.

Notes:
- Avoids sending Content-Type: application/json when json body is None (Render complains "invalid JSON").
- trigger_deploy sends an explicit JSON body (even empty {}) to avoid invalid JSON.
- get_logs uses GET /v1/logs?resourceId=... to avoid 404.
- create_service requires ownerId: we fetch owner via GET /v1/users and use that value (bot asks user for confirm).
"""

import requests
import logging
from typing import Tuple, Any, Dict, Optional, List

logger = logging.getLogger("render_api")
logger.setLevel(logging.INFO)

BASE = "https://api.render.com/v1"


class RenderAPI:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json"
        })
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def _request(self, method: str, path: str, params: Dict[str, Any] = None,
                 json_data: Any = None, headers: Dict[str, str] = None, timeout: int = 20) -> Tuple[bool, Any]:
        url = BASE + path
        h = {}
        if headers:
            h.update(headers)

        # if json_data is None -> don't set Content-Type (Render errors on empty JSON)
        if json_data is not None:
            h["Content-Type"] = "application/json"

        try:
            resp = self.session.request(method=method, url=url, params=params, json=json_data, headers=h, timeout=timeout)
        except requests.RequestException as e:
            logger.exception("HTTP request failure %s %s", method, url)
            return False, str(e)

        # try parse JSON, else text
        try:
            data = resp.json()
        except ValueError:
            data = resp.text

        if 200 <= resp.status_code < 300:
            return True, data
        else:
            logger.debug("Render API error %s %s -> %s", resp.status_code, url, data)
            return False, {"status_code": resp.status_code, "body": data}

    # ----- auth / owner -----
    def test_key(self) -> Tuple[bool, Any]:
        """Test key by fetching user (GET /v1/users)."""
        return self._request("GET", "/users")

    def owner(self) -> Tuple[bool, Any]:
        """Return user / owner info."""
        return self._request("GET", "/users")

    # ----- services -----
    def list_services(self, limit: int = 50) -> Tuple[bool, Any]:
        """List services. Render API supports limit but not offset."""
        return self._request("GET", "/services", params={"limit": limit})

    def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}")

    def delete_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/services/{service_id}")

    def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        # send {} to avoid invalid JSON
        return self._request("POST", f"/services/{service_id}/restart", json_data={})

    # ----- deploys -----
    def list_deploys(self, service_id: str, limit: int = 20) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/deploys", params={"limit": limit})

    def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        body = {"clearCache": bool(clear_cache)}
        return self._request("POST", f"/services/{service_id}/deploys", json_data=body)

    def get_deploy(self, service_id: str, deploy_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/deploys/{deploy_id}")

    # ----- logs -----
    def get_logs(self, service_id: str, tail: int = 200) -> Tuple[bool, Any]:
        # GET /v1/logs?resourceId=<service_id>&limit=<n>
        params = {"resourceId": service_id, "limit": tail}
        return self._request("GET", "/logs", params=params)

    # ----- env vars -----
    def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        # GET /services/{id}/env-vars
        return self._request("GET", f"/services/{service_id}/env-vars")

    def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        """
        Upsert env vars. Render's docs vary; using PUT /services/{id}/env-vars with array of {key, value}.
        """
        body: List[Dict[str, str]] = []
        for k, v in kv.items():
            body.append({"key": k, "value": v})
        return self._request("PUT", f"/services/{service_id}/env-vars", json_data=body)

    def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/services/{service_id}/env-vars/{key_name}")

    # ----- create / update service -----
    def create_service(self, name: str, repo: str, owner_id: str, branch: str = "main",
                       service_type: str = "web", env: str = "production",
                       build_command: Optional[str] = None, start_command: Optional[str] = None,
                       plan: Optional[str] = None) -> Tuple[bool, Any]:
        """
        Create a git-backed service. ownerId is required. Adjust fields for other service types.
        """
        payload: Dict[str, Any] = {
            "name": name,
            "repo": repo,
            "branch": branch,
            "type": service_type,
            "env": env,
            "ownerId": owner_id
        }
        if build_command:
            payload["buildCommand"] = build_command
        if start_command:
            payload["startCommand"] = start_command
        if plan:
            payload["plan"] = plan
        return self._request("POST", "/services", json_data=payload)

    def update_service(self, service_id: str, update_fields: Dict[str, Any]) -> Tuple[bool, Any]:
        """
        Generic service update. Use to set startCommand, repo, branch etc.
        """
        return self._request("PATCH", f"/services/{service_id}", json_data=update_fields)

    # ----- helpers -----
    @staticmethod
    def extract_service_url(service_obj: Dict[str, Any]) -> Optional[str]:
        if not isinstance(service_obj, dict):
            return None
        candidates = [
            service_obj.get("defaultDomain"),
            (service_obj.get("serviceDetails") or {}).get("defaultDomain"),
            (service_obj.get("serviceDetails") or {}).get("url"),
            service_obj.get("url"),
            service_obj.get("externalUrl"),
        ]
        for c in candidates:
            if c:
                return c
        return None
