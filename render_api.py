# render_api.py
import json
from typing import Dict, Any, List, Optional, Tuple, Union
import requests

BASE_URL = "https://api.render.com/v1"

class RenderAPI:
    """
    Thin wrapper around Render REST API.
    NOTE:
      - Some endpoints can vary by account/feature flags. All are centralized here
        so you can tweak easily if any 404/422 appears.
      - Timeouts & error handling included.
    """

    def __init__(self, api_key: str, timeout: int = 25):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    # -------------------------
    # Helpers
    # -------------------------
    def _ok(self, r: requests.Response) -> Tuple[bool, Union[Dict[str, Any], List[Any], str]]:
        if r.headers.get("content-type", "").startswith("application/json"):
            body: Union[Dict[str, Any], List[Any]] = {}
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text}
        else:
            body = r.text

        if 200 <= r.status_code < 300:
            return True, body
        return False, body

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None):
        r = self.session.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        return self._ok(r)

    def _post(self, path: str, data: Optional[Dict[str, Any]] = None):
        r = self.session.post(f"{BASE_URL}{path}", data=json.dumps(data or {}), timeout=self.timeout)
        return self._ok(r)

    def _delete(self, path: str):
        r = self.session.delete(f"{BASE_URL}{path}", timeout=self.timeout)
        return self._ok(r)

    def _patch(self, path: str, data: Optional[Dict[str, Any]] = None):
        r = self.session.patch(f"{BASE_URL}{path}", data=json.dumps(data or {}), timeout=self.timeout)
        return self._ok(r)

    # -------------------------
    # Auth / Basic
    # -------------------------
    def test_key(self) -> Tuple[bool, Any]:
        """Checks if key can list at least one service."""
        ok, data = self._get("/services", params={"limit": 1})
        return ok, data

    def owner(self) -> Tuple[bool, Any]:
        """
        Fetch owner/account. Some orgs use /owner; some use /accounts.
        We'll try /owner first, then fallback to /accounts.
        """
        ok, data = self._get("/owner")
        if ok:
            return ok, data
        # Fallback:
        ok2, data2 = self._get("/accounts")
        if ok2 and isinstance(data2, list) and data2:
            # Normalize a bit
            return True, {"id": data2[0].get("id"), "name": data2[0].get("name"), "email": data2[0].get("email")}
        return False, data

    # -------------------------
    # Services / Apps
    # -------------------------
    def list_services(self) -> Tuple[bool, Any]:
        return self._get("/services")

    def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._get(f"/services/{service_id}")

    def delete_service(self, service_id: str) -> Tuple[bool, Any]:
        return self._delete(f"/services/{service_id}")

    def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        # Official restart endpoint
        return self._post(f"/services/{service_id}/restart", data={})

    # -------------------------
    # Deploys
    # -------------------------
    def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        # Triggers a fresh deploy of the current service configuration (Git repo/branch already linked in Render)
        payload = {"clearCache": clear_cache}
        return self._post(f"/services/{service_id}/deploys", data=payload)

    def list_deploys(self, service_id: str, limit: int = 10) -> Tuple[bool, Any]:
        return self._get(f"/services/{service_id}/deploys", params={"limit": limit})

    # -------------------------
    # Logs
    # -------------------------
    def get_logs(self, service_id: str, tail_lines: int = 200) -> Tuple[bool, Any]:
        """
        Render logs endpoint shape may vary.
        This commonly works:
          GET /services/{serviceId}/logs?tail=200
        """
        return self._get(f"/services/{service_id}/logs", params={"tail": tail_lines})

    # -------------------------
    # Environment Variables
    # -------------------------
    def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        """
        Many accounts: GET /services/{serviceId}/env-vars
        """
        return self._get(f"/services/{service_id}/env-vars")

    def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        """
        Upsert one or more env vars.
        Common pattern: POST /services/{serviceId}/env-vars  with {"envVars":[{"key":"K","value":"V"}]}
        """
        payload = {"envVars": [{"key": k, "value": v} for k, v in kv.items()]}
        return self._post(f"/services/{service_id}/env-vars", data=payload)

    def delete_env_var(self, service_id: str, key: str) -> Tuple[bool, Any]:
        """
        Some setups support: DELETE /services/{serviceId}/env-vars/{KEY}
        """
        return self._delete(f"/services/{service_id}/env-vars/{key}")

    # -------------------------
    # GitHub (info-only trigger)
    # -------------------------
    def set_repo(self, service_id: str, repo: str, branch: str = "main", build_command: Optional[str] = None) -> Tuple[bool, Any]:
        """
        Update service with repo & branch.
        PATCH /services/{serviceId}
        """
        data: Dict[str, Any] = {
            "repo": repo,
            "branch": branch,
        }
        if build_command:
            data["buildCommand"] = build_command
        return self._patch(f"/services/{service_id}", data=data)
