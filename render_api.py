# render_api.py
"""
Render API async wrapper using httpx.
Handles:
- GET /owners for ownerId resolution (prefers team)
- GET /services, GET /services/{id}
- POST /services (create) with ownerId
- PATCH /services/{id} (update)
- POST /services/{id}/deploys (trigger deploy) -> sends {} to avoid invalid JSON
- POST /services/{id}/restart (restart) -> sends {}
- GET /services/{id}/logs (logs)
- env vars: GET/PUT/DELETE /services/{id}/env-vars
All methods return (ok: bool, data: Any)
"""
from typing import Any, Dict, Optional, Tuple
import httpx
import logging

logger = logging.getLogger("render_api")
logger.setLevel(logging.INFO)

BASE = "https://api.render.com/v1"


class RenderAPI:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _request(self, method: str, path: str, params: Dict[str, Any] = None,
                       json_data: Any = None, timeout: int = 30) -> Tuple[bool, Any]:
        url = BASE + path
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # only attach json if not None (Render complains on empty JSON sometimes)
                if json_data is not None:
                    resp = await client.request(method, url, params=params, json=json_data, headers=headers)
                else:
                    resp = await client.request(method, url, params=params, headers=headers)
        except httpx.RequestError as e:
            logger.exception("Request error %s %s", method, url)
            return False, {"error": str(e)}
        # parse
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        if 200 <= resp.status_code < 300:
            return True, data
        else:
            logger.debug("Render API error %s %s -> %s", resp.status_code, url, data)
            return False, {"status_code": resp.status_code, "body": data}

    # owners -> resolve ownerId
    async def owners(self) -> Tuple[bool, Any]:
        return await self._request("GET", "/owners")

    async def resolve_owner_id(self) -> Tuple[Optional[str], Any]:
        ok, data = await self.owners()
        if not ok or not isinstance(data, list):
            return None, data
        # prefer team owner
        for item in data:
            owner = item.get("owner") if isinstance(item, dict) else None
            if owner and owner.get("type") == "team":
                return owner.get("id"), data
        for item in data:
            owner = item.get("owner") if isinstance(item, dict) else None
            if owner and owner.get("type") == "user":
                return owner.get("id"), data
        return None, data

    # services
    async def list_services(self, limit: int = 50) -> Tuple[bool, Any]:
        return await self._request("GET", "/services", params={"limit": limit})

    async def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request("GET", f"/services/{service_id}")

    async def create_service(self, name: str, repo: str, owner_id: str, branch: str = "main",
                             service_type: str = "web", env: str = "docker",
                             start_command: Optional[str] = None, build_command: Optional[str] = None,
                             plan: Optional[str] = None) -> Tuple[bool, Any]:
        payload: Dict[str, Any] = {
            "name": name,
            "repo": repo,
            "branch": branch,
            "type": service_type,
            "env": env,
            "ownerId": owner_id
        }
        if start_command:
            payload["startCommand"] = start_command
        if build_command:
            payload["buildCommand"] = build_command
        if plan:
            payload["plan"] = plan
        # For safety, POST /services with json payload
        return await self._request("POST", "/services", json_data=payload)

    async def update_service(self, service_id: str, update_fields: Dict[str, Any]) -> Tuple[bool, Any]:
        return await self._request("PATCH", f"/services/{service_id}", json_data=update_fields)

    # deploys/restart
    async def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        body = {"clearCache": bool(clear_cache)}
        return await self._request("POST", f"/services/{service_id}/deploys", json_data=body)

    async def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        # Render supports /restart - some accounts use /deploys; keep both available (we call restart endpoint)
        return await self._request("POST", f"/services/{service_id}/restart", json_data={})

    # logs
    async def get_logs(self, service_id: str, tail: bool = True, limit: int = 200) -> Tuple[bool, Any]:
        params = {"tail": "true" if tail else "false", "limit": limit}
        return await self._request("GET", f"/services/{service_id}/logs", params=params)

    # env vars
    async def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request("GET", f"/services/{service_id}/env-vars")

    async def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        body = [{"key": k, "value": v} for k, v in kv.items()]
        return await self._request("PUT", f"/services/{service_id}/env-vars", json_data=body)

    async def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        return await self._request("DELETE", f"/services/{service_id}/env-vars/{key_name}")
