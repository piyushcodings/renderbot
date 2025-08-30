# render_api.py
"""
Tiny Render API wrapper used by bot.py

Fixes / behaviors:
- Uses Bearer auth header
- _request removes Content-Type header when no json body to avoid Render's "invalid JSON"
- trigger_deploy sends {} where Render expects JSON body
- get_logs uses GET /v1/logs with resourceId param
- owner() calls GET /v1/users
- create_service() provided (note: Render requires many fields for some service types;
  this helper sends a simple payload — adjust per your account/plan)
"""

import requests
import logging
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger("render_api")
logger.setLevel(logging.INFO)

BASE = "https://api.render.com/v1"


class RenderAPI:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
        self.session = requests.Session()
        # default headers; we'll modify per-request if needed
        self.default_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else ""
        }

    def _request(self, method: str, path: str, params: Dict[str, Any] = None,
                 json_data: Any = None, extra_headers: Dict[str, str] = None) -> Tuple[bool, Any]:
        url = BASE + path
        headers = dict(self.default_headers)
        if extra_headers:
            headers.update(extra_headers)

        # Important: if json_data is None, do not set Content-Type header (Render will error on empty body)
        # If we need to send an empty JSON object, pass json_data = {} explicitly.
        if json_data is not None:
            headers["Content-Type"] = "application/json"
        else:
            headers.pop("Content-Type", None)

        try:
            resp = self.session.request(method=method, url=url, params=params, json=json_data, headers=headers, timeout=20)
        except requests.RequestException as e:
            logger.exception("HTTP request failed: %s %s", method, url)
            return False, str(e)

        # parse JSON if possible
        try:
            data = resp.json()
        except ValueError:
            data = resp.text

        if 200 <= resp.status_code < 300:
            return True, data
        else:
            # include status and body where helpful
            logger.debug("Render API error %s %s -> %s", resp.status_code, url, data)
            return False, {"status_code": resp.status_code, "body": data}

    # --- helper endpoints ---
    def test_key(self) -> Tuple[bool, Any]:
        """Simple call to list services to test if the key works (or GET /users)."""
        if not self.api_key:
            return False, "No API key provided"
        return self._request("GET", "/users")

    def owner(self) -> Tuple[bool, Any]:
        """Get authenticated user info"""
        if not self.api_key:
            return False, "No API key provided"
        return self._request("GET", "/users")

    # --- services ---
    def list_services(self, limit: int = 50, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/services", params={"limit": limit, "offset": offset})

    def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}")

    def delete_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/services/{service_id}")

    def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        # Render supports /services/{id}/restart
        return self._request("POST", f"/services/{service_id}/restart", json_data={})  # send {} to avoid invalid JSON

    # --- deploys ---
    def list_deploys(self, service_id: str, limit: int = 20) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/deploys", params={"limit": limit})

    def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        # Render expects a JSON body for deploy trigger; sending empty object {} avoids "invalid JSON"
        body = {"clearCache": bool(clear_cache)}
        return self._request("POST", f"/services/{service_id}/deploys", json_data=body)

    def get_deploy(self, service_id: str, deploy_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/deploys/{deploy_id}")

    # --- logs ---
    def get_logs(self, service_id: str, tail: int = 200) -> Tuple[bool, Any]:
        # Use /v1/logs with resourceId param per Render docs
        params = {"resourceId": service_id, "limit": tail}
        ok, res = self._request("GET", "/logs", params=params)
        return ok, res

    # --- env vars ---
    def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/services/{service_id}/env-vars")

    def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        """
        Upsert environment variables for a service.
        Render expects a list of objects in some flows; but the API endpoint for service env vars uses PUT with
        key/value pairs: We'll use the 'put' 'env-vars' endpoint format where request body is array of {key, value}
        or per docs may accept an object. We'll pass a list to be safe.
        """
        body = []
        for k, v in kv.items():
            body.append({"key": k, "value": v})
        return self._request("PUT", f"/services/{service_id}/env-vars", json_data=body)

    def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        # DELETE /services/{serviceId}/env-vars/{key}
        return self._request("DELETE", f"/services/{service_id}/env-vars/{key_name}")

    # --- create service (basic wrapper) ---
    def create_service(self, name: str, repo: str, branch: str = "main", service_type: str = "web", env: str = "production", build_command: Optional[str] = None, start_command: Optional[str] = None, plan: Optional[str] = None) -> Tuple[bool, Any]:
        """
        Create a service. Render requires many fields depending on service type.
        This helper sends a minimal payload for Git-backed web services.
        Adjust fields for your use-case (static site, private service, background worker, docker).
        """
        payload: Dict[str, Any] = {
            "name": name,
            # when using GitHub repo, Render expects repo in format "github.com/USER/REPO" or full git URL
            "repo": repo,
            "branch": branch,
            "type": service_type,  # e.g., "web", "static", "worker" - check Render docs / dashboard values
            "env": env,
        }
        if build_command:
            payload["buildCommand"] = build_command
        if start_command:
            payload["startCommand"] = start_command
        if plan:
            payload["plan"] = plan

        return self._request("POST", "/services", json_data=payload)

    # --- helper to extract common service URL (best-effort) ---
    @staticmethod
    def extract_service_url(service_obj: Dict[str, Any]) -> Optional[str]:
        """
        Try several fields that Render commonly returns to provide a public URL.
        Returns the first found URL or None.
        """
        if not isinstance(service_obj, dict):
            return None
        # common fields to try (best-effort)
        candidates = [
            service_obj.get("defaultDomain"),
            service_obj.get("serviceDetails", {}).get("defaultDomain") if service_obj.get("serviceDetails") else None,
            service_obj.get("serviceDetails", {}).get("domain") if service_obj.get("serviceDetails") else None,
            service_obj.get("serviceDetails", {}).get("url") if service_obj.get("serviceDetails") else None,
            service_obj.get("url"),
            service_obj.get("externalUrl"),
        ]
        for c in candidates:
            if c:
                return c
        return None
