# bot.py
"""
Render Manager Bot - main bot file.

Requires environment variables:
- BOT_TOKEN
- API_ID
- API_HASH

Files:
- state.json (auto-created)

Usage:
- /login <RENDER_API_KEY>
- /start shows inline menu
- Use inline buttons to list apps, view app menu, logs, restart, deploy, create app, set repo/start
- /create <NAME> | <GIT_REPO> | <branch optional> | <startCommand optional>  <-- quick create
"""
import os
import json
import logging
from typing import Any, Dict, Optional, List

import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums import ParseMode

from render_api import RenderAPI

# -------- CONFIG --------

BOT_TOKEN = "8298721017:AAHquRSfWT5fk9DnN0clpH84jT6UTjeoBmc"
API_ID = 23907288
API_HASH = "f9a47570ed19aebf8eb0f0a5ec1111e5"
STATE_FILE = os.getenv("STATE_FILE", "state.json")
# ------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("render_manager_bot")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.warning("BOT_TOKEN/API_ID/API_HASH not set. Bot may not run properly without them.")

app = Client("render_manager", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- state ----------
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state: Dict[str, Any] = json.load(f)
    except Exception:
        state = {"api_keys": {}, "repos": {}, "_pending": {}}
else:
    state = {"api_keys": {}, "repos": {}, "_pending": {}}


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        logger.exception("Failed to save state")


def set_user_key(user_id: int, key: str):
    state.setdefault("api_keys", {})[str(user_id)] = key
    save_state()


def get_user_key(user_id: int) -> Optional[str]:
    return state.get("api_keys", {}).get(str(user_id))


def set_repo_mapping(service_id: str, repo: str, branch: str = "main", start_command: Optional[str] = None):
    state.setdefault("repos", {})[service_id] = {"repo": repo, "branch": branch, "startCommand": start_command}
    save_state()


def get_repo_mapping(service_id: str):
    return state.get("repos", {}).get(service_id)


def set_pending(user_id: int, entry: dict):
    state.setdefault("_pending", {})[str(user_id)] = entry
    save_state()


def pop_pending(user_id: int) -> Optional[dict]:
    return state.setdefault("_pending", {}).pop(str(user_id), None)


# ---------- helpers ----------
def html_escape(s: str) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


async def safe_edit(msg_obj: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    try:
        await msg_obj.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except errors.MessageNotModified:
        return
    except errors.RPCError as e:
        es = str(e)
        logger.debug("safe_edit RPCError: %s", es)
        if "ENTITY_BOUNDS_INVALID" in es or "Invalid parse mode" in es or "entities" in es:
            try:
                await msg_obj.reply_text(text, reply_markup=reply_markup)
            except Exception:
                logger.exception("Fallback reply_text failed")
        else:
            logger.exception("RPCError in safe_edit")
    except Exception:
        logger.exception("Unexpected error in safe_edit")


async def safe_reply(msg_obj: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    try:
        await msg_obj.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except errors.RPCError as e:
        es = str(e)
        logger.debug("safe_reply RPCError: %s", es)
        if "ENTITY_BOUNDS_INVALID" in es or "Invalid parse mode" in es or "entities" in es:
            try:
                await msg_obj.reply_text(text, reply_markup=reply_markup)
            except Exception:
                logger.exception("Fallback plain reply_text failed")
        else:
            logger.exception("Unexpected RPCError in safe_reply")
    except Exception:
        logger.exception("Unexpected error in safe_reply")


# ---------- keyboards ----------
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Account", callback_data="account")],
        [InlineKeyboardButton("📋 List Apps", callback_data="list_apps")],
        [InlineKeyboardButton("➕ Create App", callback_data="create_root")],
        [InlineKeyboardButton("🗂 Repo Mappings", callback_data="repo_mappings")],
    ])


def service_menu_kb(sid: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Status", callback_data=f"svc_status:{sid}")],
        [InlineKeyboardButton("🪵 Logs", callback_data=f"svc_logs:{sid}:1"), InlineKeyboardButton("🔄 Restart", callback_data=f"svc_restart:{sid}")],
        [InlineKeyboardButton("🔗 Set Repo/Start", callback_data=f"svc_repo_set:{sid}"), InlineKeyboardButton("🚀 Deploy", callback_data=f"svc_deploy:{sid}")],
        [InlineKeyboardButton("🌐 Env Vars", callback_data=f"svc_env:{sid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="list_apps")],
    ])


def logs_nav_kb(sid: str, page: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Prev", callback_data=f"svc_logs:{sid}:{max(1, page-1)}"),
         InlineKeyboardButton("▶️ Next", callback_data=f"svc_logs:{sid}:{page+1}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"svc:{sid}")]
    ])


# ---------- bot commands ----------
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    text = ("<b>Render Manager Bot</b>\n\n"
            "1. /login <RENDER_API_KEY> — save your Render API key (private)\n"
            "2. Use the inline menu to manage services (list apps, create, logs, deploy).\n")
    await safe_reply(message, text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("login"))
async def cmd_login(_, message: Message):
    if len(message.command) < 2:
        await safe_reply(message, "Usage: /login <RENDER_API_KEY>")
        return
    key = message.command[1].strip()
    api = RenderAPI(key)
    ok, owners = await api.owners()
    if not ok:
        await safe_reply(message, f"❌ Invalid key or API unreachable.\n{owners}")
        return
    # save key
    set_user_key(message.from_user.id, key)
    await safe_reply(message, "✅ Render API key saved. Use the menu below.", reply_markup=main_menu_kb())


@app.on_message(filters.command("whoami"))
async def cmd_whoami(_, message: Message):
    key = get_user_key(message.from_user.id)
    if not key:
        await safe_reply(message, "Not logged in. Use /login <RENDER_API_KEY>")
        return
    api = RenderAPI(key)
    ok, owners = await api.owners()
    if not ok:
        await safe_reply(message, f"❌ Failed to fetch owners.\n{owners}")
        return
    lines = []
    if isinstance(owners, list):
        for item in owners:
            owner = item.get("owner") if isinstance(item, dict) else None
            if owner:
                lines.append(f"{owner.get('type')} • {owner.get('name')} • {owner.get('id')}")
    else:
        lines.append(str(owners))
    await safe_reply(message, "<b>Owners / Workspaces</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------- callback handler ----------
@app.on_callback_query()
async def on_cb(client: Client, callback: CallbackQuery):
    data = callback.data or ""
    user_id = callback.from_user.id
    api_key = get_user_key(user_id)
    api = RenderAPI(api_key) if api_key else None

    # ACCOUNT
    if data == "account":
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb())
            return
        ok, owners = await api.owners()
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch owners.\n{owners}", reply_markup=main_menu_kb())
            return
        out = []
        if isinstance(owners, list):
            for item in owners:
                owner = item.get("owner") if isinstance(item, dict) else None
                if owner:
                    out.append(f"<b>{html_escape(owner.get('name'))}</b>\nType: {html_escape(owner.get('type'))}\nID: <code>{html_escape(owner.get('id'))}</code>")
        else:
            out.append(html_escape(str(owners)))
        await safe_edit(callback.message, "👤 Account / Owners:\n\n" + "\n\n".join(out), reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    # LIST APPS
    if data == "list_apps":
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb())
            return
        ok, svcs = await api.list_services(limit=200)
        if not ok:
            await safe_edit(callback.message, f"❌ Failed to list services.\n{svcs}", reply_markup=main_menu_kb())
            return
        items = svcs if isinstance(svcs, list) else (svcs.get("services") if isinstance(svcs, dict) else [])
        if not items:
            await safe_edit(callback.message, "No services found.", reply_markup=main_menu_kb())
            return
        rows = []
        for s in items:
            svc = s.get("service") if isinstance(s, dict) and "service" in s else s
            sid = svc.get("id") if isinstance(svc, dict) else (s.get("id"))
            name = svc.get("name") if isinstance(svc, dict) else str(s)
            url = svc.get("defaultDomain") or (svc.get("serviceDetails") or {}).get("defaultDomain") or ""
            label = f"📱 {name}" + (f" → {url}" if url else "")
            rows.append([InlineKeyboardButton(label, callback_data=f"svc:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
        await safe_edit(callback.message, "📋 <b>Your Services</b>:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    # CREATE ROOT
    if data == "create_root":
        await safe_edit(callback.message, "🔧 To create a service use the chat command:\n\n`/create <NAME> | <GIT_REPO_URL> | <branch optional> | <startCommand optional>`\n\nExample:\n`/create my-app | https://github.com/me/repo | main | npm start`", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    # REPO MAPPINGS
    if data == "repo_mappings":
        rows = []
        for sid, info in state.get("repos", {}).items():
            rows.append([InlineKeyboardButton(f"{sid} • {info.get('repo')}@{info.get('branch')}", callback_data=f"repo_info:{sid}")])
        if not rows:
            await safe_edit(callback.message, "No repo mappings stored.", reply_markup=main_menu_kb())
        else:
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
            await safe_edit(callback.message, "<b>Repo mappings</b>:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    # repo_info
    if data.startswith("repo_info:"):
        sid = data.split(":", 1)[1]
        mapping = get_repo_mapping(sid)
        if not mapping:
            await safe_edit(callback.message, "No mapping found.", reply_markup=main_menu_kb())
            return
        text = f"<b>Service {sid}</b>\nRepo: <code>{html_escape(mapping.get('repo'))}</code>\nBranch: <code>{html_escape(mapping.get('branch'))}</code>\nStart: <code>{html_escape(mapping.get('startCommand'))}</code>"
        await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="repo_mappings")]]), parse_mode=ParseMode.HTML)
        return

    # SERVICE entry
    if data.startswith("svc:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb())
            return
        ok, svc = await api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch service:\n{svc}", reply_markup=main_menu_kb())
            return
        service = svc.get("service") if isinstance(svc, dict) and "service" in svc else svc
        name = service.get("name") if isinstance(service, dict) else str(service)
        stype = service.get("type") or "-"
        status = (service.get("serviceDetails") or {}).get("status") or service.get("status") or "-"
        url = service.get("defaultDomain") or (service.get("serviceDetails") or {}).get("defaultDomain") or "(no public url)"
        text = f"<b>{html_escape(name)}</b>\nID: <code>{html_escape(sid)}</code>\nType: {html_escape(stype)}\nStatus: {html_escape(status)}\nURL: {html_escape(url)}"
        await safe_edit(callback.message, text, reply_markup=service_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # SERVICE status
    if data.startswith("svc_status:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb())
            return
        ok, svc = await api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch status.\n{svc}", reply_markup=service_menu_kb(sid))
            return
        service = svc.get("service") if isinstance(svc, dict) and "service" in svc else svc
        status = (service.get("serviceDetails") or {}).get("status") or service.get("status") or "-"
        await safe_edit(callback.message, f"<b>Status</b>\n{html_escape(status)}", reply_markup=service_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # LOGS navigation: svc_logs:<sid>:<page>
    if data.startswith("svc_logs:"):
        try:
            _, sid, page_s = data.split(":", 2)
            page = int(page_s)
        except Exception:
            await safe_edit(callback.message, "Invalid logs request.", reply_markup=main_menu_kb())
            return
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb())
            return
        limit = 50
        ok, logs_resp = await api.get_logs(sid, tail=True, limit=limit * page)
        if not ok:
            await safe_edit(callback.message, f"❌ Logs fetch failed.\n{logs_resp}", reply_markup=service_menu_kb(sid))
            return
        logs_list = []
        if isinstance(logs_resp, dict) and "logs" in logs_resp:
            logs_list = logs_resp["logs"]
        elif isinstance(logs_resp, list):
            logs_list = logs_resp
        else:
            await safe_edit(callback.message, f"❌ Unexpected logs format.\n{logs_resp}", reply_markup=service_menu_kb(sid))
            return
        # compute slice for page (newest logs show last)
        start = max(0, len(logs_list) - (limit * page))
        end = len(logs_list) - (limit * (page - 1))
        page_lines = logs_list[start:end]
        if not page_lines:
            text = "(no logs found for this page)"
        else:
            formatted = []
            for l in page_lines:
                ts = l.get("timestamp") or l.get("time") or ""
                stream = l.get("stream") or l.get("type") or ""
                msg = l.get("message") or l.get("log") or str(l)
                formatted.append(f"[{ts}] {stream}: {msg}")
            text = "\n".join(formatted[-limit:])
        if len(text) > 3900:
            text = text[-3900:]
            text = "(truncated)\n" + text
        await safe_edit(callback.message, "<b>Logs</b>\n<pre>" + html_escape(text) + "</pre>", reply_markup=logs_nav_kb(sid, page), parse_mode=ParseMode.HTML)
        return

    # RESTART
    if data.startswith("svc_restart:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu_kb(sid))
            return
        await safe_edit(callback.message, "⏳ Restarting...", reply_markup=None)
        ok, res = await api.restart_service(sid)
        if ok:
            await safe_edit(callback.message, "✅ Restart triggered.", reply_markup=service_menu_kb(sid))
        else:
            await safe_edit(callback.message, f"❌ Restart failed.\n{res}", reply_markup=service_menu_kb(sid))
        return

    # DEPLOY
    if data.startswith("svc_deploy:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu_kb(sid))
            return
        await safe_edit(callback.message, "🚀 Triggering deploy...", reply_markup=None)
        ok, res = await api.trigger_deploy(sid)
        if ok:
            dep_id = res.get("id") if isinstance(res, dict) else "-"
            await safe_edit(callback.message, f"✅ Deploy triggered.\nDeploy ID: <code>{html_escape(str(dep_id))}</code>", reply_markup=service_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_edit(callback.message, f"❌ Deploy failed.\n{res}", reply_markup=service_menu_kb(sid))
        return

    # SET REPO/START flow: mark pending and instruct user
    if data.startswith("svc_repo_set:"):
        sid = data.split(":", 1)[1]
        set_pending(user_id, {"type": "set_repo", "service_id": sid})
        await safe_edit(callback.message, "✏️ Send repo & branch & optional start command in a private message like:\n`https://github.com/user/repo | main | npm start`\n\n(Branch and start command optional)\n\nOr use `/setrepo <service_id> | <repo> | <branch> | <startCommand>`", reply_markup=service_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # ENV VARS root
    if data.startswith("svc_env:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu_kb(sid))
            return
        ok, res = await api.list_env_vars(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not list env vars.\n{res}", reply_markup=service_menu_kb(sid))
            return
        pairs = []
        if isinstance(res, dict) and "envVars" in res:
            pairs = res["envVars"]
        elif isinstance(res, list):
            pairs = res
        lines = []
        for p in pairs:
            k = p.get("key") or p.get("name") or p.get("keyName")
            v = p.get("value") or ""
            lines.append(f"{html_escape(k)} = {html_escape(v)}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add/Update", callback_data=f"env_add:{sid}")],
                                   [InlineKeyboardButton("➖ Delete", callback_data=f"env_del:{sid}")],
                                   [InlineKeyboardButton("⬅️ Back", callback_data=f"svc:{sid}")]])
        await safe_edit(callback.message, "<b>Env Vars</b>\n" + ("\n".join(lines) if lines else "(none)"), reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # env add
    if data.startswith("env_add:"):
        sid = data.split(":", 1)[1]
        set_pending(user_id, {"type": "env_add", "service_id": sid})
        await safe_edit(callback.message, "Send env var lines like:\nKEY=VALUE\nANOTHER=VAL\n(Will upsert)", reply_markup=service_menu_kb(sid))
        return

    # env delete
    if data.startswith("env_del:"):
        sid = data.split(":", 1)[1]
        set_pending(user_id, {"type": "env_del", "service_id": sid})
        await safe_edit(callback.message, "Send the ENV KEY name you want to delete (exact):", reply_markup=service_menu_kb(sid))
        return

    # fallback
    await safe_edit(callback.message, "Unknown action. Use /start.", reply_markup=main_menu_kb())


# ---------- text handler for pending flows & commands ----------
@app.on_message(filters.private & filters.text)
async def on_private_text(client: Client, message: Message):
    txt = message.text.strip()
    user_id = message.from_user.id

    # direct commands: /create, /setrepo, /logs, /login
    if txt.startswith("/create "):
        parts = [p.strip() for p in txt[len("/create "):].split("|")]
        if len(parts) < 2:
            await safe_reply(message, "Usage: /create <NAME> | <GIT_REPO_URL> | <branch optional> | <startCommand optional>")
            return
        name = parts[0]
        repo = parts[1]
        branch = parts[2] if len(parts) >= 3 and parts[2] else "main"
        start_cmd = parts[3] if len(parts) >= 4 and parts[3] else None
        key = get_user_key(user_id)
        if not key:
            await safe_reply(message, "Please /login first with your Render API key.")
            return
        api = RenderAPI(key)
        # resolve ownerId
        owner_id, raw = await api.resolve_owner_id()
        if not owner_id:
            await safe_reply(message, f"❌ Could not determine ownerId. Raw:\n{raw}")
            return
        await safe_reply(message, "🛠 Creating service...")
        ok, res = await api.create_service(name=name, repo=repo, owner_id=owner_id, branch=branch, start_command=start_cmd)
        if ok:
            sid = res.get("id") or (res.get("service") or {}).get("id")
            if sid:
                set_repo_mapping(sid, repo, branch, start_cmd)
            url = (res.get("defaultDomain") or (res.get("service") or {}).get("defaultDomain")) if isinstance(res, dict) else ""
            await safe_reply(message, f"✅ Created service.\nID: <code>{html_escape(str(sid))}</code>\nURL: {html_escape(str(url))}", parse_mode=ParseMode.HTML)
        else:
            await safe_reply(message, f"❌ Create failed.\n{res}")
        return

    if txt.startswith("/setrepo "):
        parts = [p.strip() for p in txt[len("/setrepo "):].split("|")]
        if len(parts) < 2:
            await safe_reply(message, "Usage: /setrepo <service_id> | <repo> | <branch optional> | <startCommand optional>")
            return
        sid = parts[0]
        repo = parts[1]
        branch = parts[2] if len(parts) >= 3 and parts[2] else "main"
        start_cmd = parts[3] if len(parts) >= 4 and parts[3] else None
        key = get_user_key(user_id)
        if not key:
            await safe_reply(message, "Please /login first.")
            return
        api = RenderAPI(key)
        update_fields = {}
        if repo:
            update_fields["repo"] = repo
        if branch:
            update_fields["branch"] = branch
        if start_cmd:
            update_fields["startCommand"] = start_cmd
        if not update_fields:
            await safe_reply(message, "No fields to update.")
            return
        await safe_reply(message, "🔧 Updating service...")
        ok, res = await api.update_service(sid, update_fields)
        if ok:
            set_repo_mapping(sid, repo, branch, start_cmd)
            await safe_reply(message, "✅ Service updated.")
        else:
            await safe_reply(message, f"❌ Update failed.\n{res}")
        return

    if txt.startswith("/login "):
        parts = txt.split(maxsplit=1)
        if len(parts) == 2:
            key = parts[1].strip()
            api = RenderAPI(key)
            ok, owners = await api.owners()
            if not ok:
                await safe_reply(message, f"❌ Invalid key or API unreachable.\n{owners}")
                return
            set_user_key(user_id, key)
            await safe_reply(message, "✅ API key saved. Use /create or inline menu.")
        return

    if txt.startswith("/logs "):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            await safe_reply(message, "Usage: /logs <service_id>")
            return
        sid = parts[1].strip()
        key = get_user_key(user_id)
        if not key:
            await safe_reply(message, "Please /login first.")
            return
        api = RenderAPI(key)
        ok, logs_resp = await api.get_logs(sid, tail=True, limit=200)
        if not ok:
            await safe_reply(message, f"❌ Logs fetch failed.\n{logs_resp}")
            return
        logs_list = []
        if isinstance(logs_resp, dict) and "logs" in logs_resp:
            logs_list = logs_resp["logs"]
        elif isinstance(logs_resp, list):
            logs_list = logs_resp
        else:
            await safe_reply(message, f"❌ Unexpected logs format.\n{logs_resp}")
            return
        formatted = []
        for l in logs_list[-200:]:
            ts = l.get("timestamp") or l.get("time") or ""
            stream = l.get("stream") or l.get("type") or ""
            msg = l.get("message") or l.get("log") or str(l)
            formatted.append(f"[{ts}] {stream}: {msg}")
        text = "\n".join(formatted[-200:])
        if len(text) > 3900:
            text = text[-3900:]
            text = "(truncated)\n" + text
        await safe_reply(message, "<b>Logs</b>\n<pre>" + html_escape(text) + "</pre>", parse_mode=ParseMode.HTML)
        return

    # pending flows
    pending = pop_pending(user_id)
    if pending:
        typ = pending.get("type")
        if typ == "set_repo":
            sid = pending.get("service_id")
            parts = [p.strip() for p in txt.split("|")]
            repo = parts[0] if parts else None
            branch = parts[1] if len(parts) >= 2 and parts[1] else "main"
            start_cmd = parts[2] if len(parts) >= 3 and parts[2] else None
            key = get_user_key(user_id)
            if not key:
                await safe_reply(message, "Please /login first.")
                return
            api = RenderAPI(key)
            update = {}
            if repo:
                update["repo"] = repo
            if branch:
                update["branch"] = branch
            if start_cmd:
                update["startCommand"] = start_cmd
            if not update:
                await safe_reply(message, "No update provided. Cancelled.")
                return
            await safe_reply(message, "🔧 Updating service...")
            ok, res = await api.update_service(sid, update)
            if ok:
                set_repo_mapping(sid, repo, branch, start_cmd)
                await safe_reply(message, "✅ Service updated.")
            else:
                await safe_reply(message, f"❌ Update failed.\n{res}")
            return
        if typ == "env_add":
            sid = pending.get("service_id")
            key = get_user_key(user_id)
            if not key:
                await safe_reply(message, "Please /login first.")
                return
            # parse KEY=VALUE lines
            kv = {}
            for line in txt.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip(); v = v.strip()
                    if k:
                        kv[k] = v
            if not kv:
                await safe_reply(message, "No valid KEY=VALUE lines found. Cancelled.")
                return
            api = RenderAPI(key)
            ok, res = await api.upsert_env_vars(sid, kv)
            if ok:
                await safe_reply(message, "✅ Env vars upserted.")
            else:
                await safe_reply(message, f"❌ Failed to upsert env vars.\n{res}")
            return
        if typ == "env_del":
            sid = pending.get("service_id")
            keyn = txt.strip()
            if not keyn:
                await safe_reply(message, "No key provided. Cancelled.")
                return
            api_key = get_user_key(user_id)
            if not api_key:
                await safe_reply(message, "Please /login first.")
                return
            api = RenderAPI(api_key)
            ok, res = await api.delete_env_var(sid, keyn)
            if ok:
                await safe_reply(message, f"✅ Env var <b>{html_escape(keyn)}</b> deleted.", parse_mode=ParseMode.HTML)
            else:
                await safe_reply(message, f"❌ Delete failed.\n{res}")
            return

    # fallback
    await safe_reply(message, "Unrecognized input. Use /help or inline menu.")


# ---------- debug helper ----------
@app.on_message(filters.command("dumpstate"))
async def dump_state(_, message: Message):
    await message.reply_text(f"state keys: {list(state.keys())}\n\n{json.dumps(state, indent=2)[:3500]}")


# ---------- run ----------
if __name__ == "__main__":
    print("🤖 Render Manager Bot starting...")
    app.run()
