# render_api.py
"""
Render API async wrapper using httpx.

- Robust request wrapper returning (ok: bool, data: Any)
- owners -> resolve_owner_id (prefers team then user)
- list_services (safe limit <= 100)
- get_service
- create_service (uses valid service types)
- update_service (PATCH)
- trigger_deploy, restart_service
- get_logs via /services/{id}/logs (tail & limit)
- env vars list/upsert/delete
- additional logs endpoints (list_logs / cursor) if required
"""
from typing import Any, Dict, Optional, Tuple
import httpx
import logging

logger = logging.getLogger("render_api")
logger.setLevel(logging.INFO)

BASE = "https://api.render.com/v1"

VALID_SERVICE_TYPES = {
    "static_site",
    "web_service",
    "private_service",
    "background_worker",
    "cron_job",
    "workflow",
}


class RenderAPI:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path: str, params: Dict[str, Any] = None,
                       json_data: Any = None, timeout: int = 30) -> Tuple[bool, Any]:
        url = BASE + path
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Only send json if provided (Render can error on empty json in some endpoints)
                if json_data is not None:
                    resp = await client.request(method, url, params=params, json=json_data, headers=self._headers())
                else:
                    resp = await client.request(method, url, params=params, headers=self._headers())
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
        # prefer team
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
        # enforce max limit 100
        params = {}
        if limit:
            params["limit"] = min(int(limit), 100)
        return await self._request("GET", "/services", params=params)

    async def get_service(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request("GET", f"/services/{service_id}")

    async def create_service(self,
                             owner_id: str,
                             name: str,
                             service_type: str,
                             repo: Optional[str] = None,
                             branch: str = "main",
                             runtime: Optional[str] = None,
                             start_command: Optional[str] = None,
                             build_command: Optional[str] = None,
                             env_vars: Optional[Dict[str, str]] = None,
                             plan: Optional[str] = None) -> Tuple[bool, Any]:
        """
        Create service.
        service_type must be one of VALID_SERVICE_TYPES.
        Only include fields that are relevant to that type.
        """
        if service_type not in VALID_SERVICE_TYPES:
            return False, {"status_code": 400, "body": {"message": f"invalid service type: {service_type}"}}

        body: Dict[str, Any] = {
            "ownerId": owner_id,
            "name": name,
            "type": service_type
        }

        # For services that require repo (web_service, private_service, background_worker, workflow)
        repo_types = {"web_service", "private_service", "background_worker", "workflow"}
        if service_type in repo_types:
            if not repo:
                return False, {"status_code": 400, "body": {"message": "repo is required for selected service type"}}
            body["repo"] = repo
            body["branch"] = branch
            if runtime:
                body["runtime"] = runtime
            if start_command:
                body["startCommand"] = start_command
            if build_command:
                body["buildCommand"] = build_command
            if env_vars:
                # API expects list of objects maybe on create - we'll include as envVars where applicable
                body["envVars"] = [{"key": k, "value": v} for k, v in env_vars.items()]

        # static_site can accept repo + branch too
        if service_type == "static_site":
            if repo:
                body["repo"] = repo
                body["branch"] = branch
            if build_command:
                body["buildCommand"] = build_command

        if plan:
            body["plan"] = plan

        return await self._request("POST", "/services", json_data=body)

    async def update_service(self, service_id: str, update_fields: Dict[str, Any]) -> Tuple[bool, Any]:
        return await self._request("PATCH", f"/services/{service_id}", json_data=update_fields)

    # deploys/restart
    async def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Any]:
        body = {"clearCache": bool(clear_cache)}
        return await self._request("POST", f"/services/{service_id}/deploys", json_data=body)

    async def restart_service(self, service_id: str) -> Tuple[bool, Any]:
        # Some accounts use /restart endpoint; try /restart first then fallback to deploy
        ok, res = await self._request("POST", f"/services/{service_id}/restart", json_data={})
        if ok:
            return True, res
        # fallback: trigger deploy (common to redeploy)
        return await self.trigger_deploy(service_id, clear_cache=False)

    # logs
    async def get_service_logs(self, service_id: str, tail: bool = True, limit: int = 100) -> Tuple[bool, Any]:
        # limit must be <= 100 (Render-side restriction)
        params = {"tail": "true" if tail else "false", "limit": min(int(limit), 100)}
        return await self._request("GET", f"/services/{service_id}/logs", params=params)

    # env vars
    async def list_env_vars(self, service_id: str) -> Tuple[bool, Any]:
        return await self._request("GET", f"/services/{service_id}/env-vars")

    async def upsert_env_vars(self, service_id: str, kv: Dict[str, str]) -> Tuple[bool, Any]:
        body = [{"key": k, "value": v} for k, v in kv.items()]
        return await self._request("PUT", f"/services/{service_id}/env-vars", json_data=body)

    async def delete_env_var(self, service_id: str, key_name: str) -> Tuple[bool, Any]:
        return await self._request("DELETE", f"/services/{service_id}/env-vars/{key_name}")

    # Generic logs listing using /logs endpoint (advanced)
    async def list_logs(self, start_time: Optional[str] = None, end_time: Optional[str] = None,
                        cursor: Optional[str] = None, limit: Optional[int] = None) -> Tuple[bool, Any]:
        params: Dict[str, Any] = {}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = min(int(limit), 100)
        return await self._request("GET", "/logs", params=params)
