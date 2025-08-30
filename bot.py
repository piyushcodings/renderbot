# bot.py
"""
Render Manager Bot - main bot file.

Features implemented/fixed:
- /start, /login <RENDER_API_KEY>
- Inline menu: Account, List Apps, Create App (choose service type via inline keyboard)
- List Apps with safe limit (<=100)
- Per-service menu: Status, Logs (pagination), Restart, Deploy, Set Repo/Start, Env Vars
- Create flow asks for service type (inline) then collects details in chat
- OwnerId resolution using GET /owners (prefers team)
- Safe parse mode handling and fallbacks for Telegram entity errors
- State persisted in state.json (api keys, repo mappings, pending flows)
- Uses render_api.RenderAPI for calls
"""
import os
import json
import logging
from typing import Any, Dict, Optional

import asyncio
from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums import ParseMode

from render_api import RenderAPI, VALID_SERVICE_TYPES

# ------------- config -------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
# ----------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("render_bot")

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.warning("BOT_TOKEN/API_ID/API_HASH are recommended to be set in environment.")

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
def html_escape(s: Optional[str]) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
                logger.exception("Fallback reply_text in safe_edit failed")
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
def main_menu():
    kb = [
        [InlineKeyboardButton("👤 Account", callback_data="account")],
        [InlineKeyboardButton("📋 List Apps", callback_data="list_apps")],
        [InlineKeyboardButton("➕ Create App", callback_data="create_root")],
        [InlineKeyboardButton("🗂 Repo Mappings", callback_data="repo_mappings")],
    ]
    return InlineKeyboardMarkup(kb)


def service_menu(sid: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Status", callback_data=f"svc_status:{sid}")],
        [InlineKeyboardButton("🪵 Logs", callback_data=f"svc_logs:{sid}:1"),
         InlineKeyboardButton("🔄 Restart", callback_data=f"svc_restart:{sid}")],
        [InlineKeyboardButton("🔗 Set Repo/Start", callback_data=f"svc_repo_set:{sid}"),
         InlineKeyboardButton("🚀 Deploy", callback_data=f"svc_deploy:{sid}")],
        [InlineKeyboardButton("🌐 Env Vars", callback_data=f"svc_env:{sid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="list_apps")],
    ])


def logs_nav(sid: str, page: int):
    kb = [
        [InlineKeyboardButton("◀️ Prev", callback_data=f"svc_logs:{sid}:{max(1, page-1)}"),
         InlineKeyboardButton("▶️ Next", callback_data=f"svc_logs:{sid}:{page+1}")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"svc:{sid}")]
    ]
    return InlineKeyboardMarkup(kb)


def service_type_kb():
    # Present a user-friendly label but callback contains actual render type
    rows = []
    label_map = {
        "Web Service": "web_service",
        "Static Site": "static_site",
        "Private Service": "private_service",
        "Background Worker": "background_worker",
        "Cron Job": "cron_job",
        "Workflow": "workflow"
    }
    for label, stype in label_map.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"create_type:{stype}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
    return InlineKeyboardMarkup(rows)


# ---------- commands ----------
@app.on_message(filters.command("start"))
async def cmd_start(_, message: Message):
    text = ("<b>Render Manager Bot</b>\n\n"
            "Commands:\n"
            "/login <RENDER_API_KEY> — save your Render API key (private)\n"
            "/create <NAME> | <GIT_REPO> | <branch optional> | <startCommand optional>\n\n"
            "Use the inline menu below.")
    await safe_reply(message, text, reply_markup=main_menu())


@app.on_message(filters.command("login"))
async def cmd_login(_, message: Message):
    if len(message.command) < 2:
        await safe_reply(message, "Usage: /login <RENDER_API_KEY>")
        return
    api_key = message.command[1].strip()
    api = RenderAPI(api_key)
    ok, data = await api.owners()
    if not ok:
        await safe_reply(message, f"❌ Invalid key or API unreachable.\n{data}")
        return
    set_user_key(message.from_user.id, api_key)
    await safe_reply(message, "✅ API key saved. Use the menu below.", reply_markup=main_menu())


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


# ---------- callbacks ----------
@app.on_callback_query()
async def on_cb(client: Client, callback: CallbackQuery):
    data = callback.data or ""
    uid = callback.from_user.id
    api_key = get_user_key(uid)
    api = RenderAPI(api_key) if api_key else None

    # ACCOUNT
    if data == "account":
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, owners = await api.owners()
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch owners.\n{owners}", reply_markup=main_menu())
            return
        out = []
        if isinstance(owners, list):
            for item in owners:
                owner = item.get("owner")
                out.append(f"<b>{html_escape(owner.get('name'))}</b>\nType: {html_escape(owner.get('type'))}\nID: <code>{html_escape(owner.get('id'))}</code>")
        else:
            out.append(html_escape(str(owners)))
        await safe_edit(callback.message, "👤 Account / Owners:\n\n" + "\n\n".join(out), reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # LIST APPS
    if data == "list_apps":
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        # safe default limit 50 (max 100)
        ok, svcs = await api.list_services(limit=50)
        if not ok:
            await safe_edit(callback.message, f"❌ Failed to list services.\n{svcs}", reply_markup=main_menu())
            return
        items = svcs if isinstance(svcs, list) else (svcs.get("services") if isinstance(svcs, dict) else [])
        if not items:
            await safe_edit(callback.message, "No services found.", reply_markup=main_menu())
            return
        rows = []
        for s in items:
            svc = s.get("service") if isinstance(s, dict) and "service" in s else s
            sid = svc.get("id") if isinstance(svc, dict) else s.get("id")
            name = svc.get("name") if isinstance(svc, dict) else str(s)
            url = svc.get("defaultDomain") or (svc.get("serviceDetails") or {}).get("defaultDomain") or ""
            label = f"📱 {name}" + (f" → {url}" if url else "")
            rows.append([InlineKeyboardButton(label, callback_data=f"svc:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
        await safe_edit(callback.message, "📋 <b>Your Services</b>:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    # CREATE ROOT -> choose service type
    if data == "create_root":
        await safe_edit(callback.message, "Select a service type to create:", reply_markup=service_type_kb())
        return

    # SERVICE TYPE chosen -> set pending and ask for create details
    if data.startswith("create_type:"):
        stype = data.split(":", 1)[1]
        if stype not in VALID_SERVICE_TYPES:
            await safe_edit(callback.message, "Invalid service type selected.", reply_markup=main_menu())
            return
        # set pending create flow
        set_pending(uid, {"type": "create", "service_type": stype})
        await safe_edit(callback.message,
                        f"Selected <b>{html_escape(stype)}</b>.\nNow send create details in this private chat using:\n\n"
                        "`/create <NAME> | <GIT_REPO or blank> | <branch optional> | <startCommand optional>`\n\n"
                        "For static_site you may omit repo if creating an empty site.\nExample:\n`/create my-app | https://github.com/me/repo | main | npm start`",
                        reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # REPO MAPPINGS
    if data == "repo_mappings":
        rows = []
        for sid, info in state.get("repos", {}).items():
            rows.append([InlineKeyboardButton(f"{sid} • {info.get('repo')}@{info.get('branch')}", callback_data=f"repo_info:{sid}")])
        if not rows:
            await safe_edit(callback.message, "No repo mappings stored.", reply_markup=main_menu())
        else:
            rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
            await safe_edit(callback.message, "<b>Repo mappings</b>:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    if data.startswith("repo_info:"):
        sid = data.split(":", 1)[1]
        mp = get_repo_mapping(sid)
        if not mp:
            await safe_edit(callback.message, "No mapping found.", reply_markup=main_menu())
            return
        txt = f"<b>Service {sid}</b>\nRepo: <code>{html_escape(mp.get('repo'))}</code>\nBranch: <code>{html_escape(mp.get('branch'))}</code>\nStart: <code>{html_escape(mp.get('startCommand'))}</code>"
        await safe_edit(callback.message, txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="repo_mappings")]]), parse_mode=ParseMode.HTML)
        return

    # SERVICE entry
    if data.startswith("svc:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, svc = await api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch service:\n{svc}", reply_markup=main_menu())
            return
        service = svc.get("service") if isinstance(svc, dict) and "service" in svc else svc
        name = service.get("name") if isinstance(service, dict) else str(service)
        stype = service.get("type") or "-"
        status = (service.get("serviceDetails") or {}).get("status") or service.get("status") or "-"
        url = service.get("defaultDomain") or (service.get("serviceDetails") or {}).get("defaultDomain") or "(no public url)"
        text = f"<b>{html_escape(name)}</b>\nID: <code>{html_escape(sid)}</code>\nType: {html_escape(stype)}\nStatus: {html_escape(status)}\nURL: {html_escape(url)}"
        await safe_edit(callback.message, text, reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # STATUS
    if data.startswith("svc_status:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, svc = await api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch status.\n{svc}", reply_markup=service_menu(sid))
            return
        service = svc.get("service") if isinstance(svc, dict) and "service" in svc else svc
        status = (service.get("serviceDetails") or {}).get("status") or service.get("status") or "-"
        await safe_edit(callback.message, f"<b>Status</b>\n{html_escape(status)}", reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # LOGS pagination
    if data.startswith("svc_logs:"):
        # svc_logs:service_id:page
        try:
            _, sid, page_s = data.split(":", 2)
            page = int(page_s)
        except Exception:
            await safe_edit(callback.message, "Invalid log request.", reply_markup=main_menu())
            return
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        # we'll pull limit * page lines and slice to last page
        limit = 50
        ok, logs_resp = await api.get_service_logs(sid, tail=True, limit=limit * page)
        if not ok:
            await safe_edit(callback.message, f"❌ Logs fetch failed.\n{logs_resp}", reply_markup=service_menu(sid))
            return
        logs_list = []
        if isinstance(logs_resp, dict) and "logs" in logs_resp:
            logs_list = logs_resp["logs"]
        elif isinstance(logs_resp, list):
            logs_list = logs_resp
        else:
            await safe_edit(callback.message, f"❌ Unexpected logs format.\n{logs_resp}", reply_markup=service_menu(sid))
            return
        start = max(0, len(logs_list) - (limit * page))
        end = len(logs_list) - (limit * (page - 1))
        page_lines = logs_list[start:end]
        if not page_lines:
            text = "(no logs for this page)"
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
        await safe_edit(callback.message, "<b>Logs</b>\n<pre>" + html_escape(text) + "</pre>", reply_markup=logs_nav(sid, page), parse_mode=ParseMode.HTML)
        return

    # RESTART
    if data.startswith("svc_restart:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu(sid))
            return
        await safe_edit(callback.message, "⏳ Restarting...", reply_markup=None)
        ok, res = await api.restart_service(sid)
        if ok:
            await safe_edit(callback.message, "✅ Restart triggered.", reply_markup=service_menu(sid))
        else:
            await safe_edit(callback.message, f"❌ Restart failed.\n{res}", reply_markup=service_menu(sid))
        return

    # DEPLOY
    if data.startswith("svc_deploy:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu(sid))
            return
        await safe_edit(callback.message, "🚀 Triggering deploy...", reply_markup=None)
        ok, res = await api.trigger_deploy(sid)
        if ok:
            dep_id = res.get("id") if isinstance(res, dict) else "-"
            await safe_edit(callback.message, f"✅ Deploy triggered.\nDeploy ID: <code>{html_escape(str(dep_id))}</code>", reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_edit(callback.message, f"❌ Deploy failed.\n{res}", reply_markup=service_menu(sid))
        return

    # SET REPO/START flow
    if data.startswith("svc_repo_set:"):
        sid = data.split(":", 1)[1]
        set_pending(uid, {"type": "set_repo", "service_id": sid})
        await safe_edit(callback.message, "✏️ Send in private chat:\n`<repo_url> | <branch optional> | <startCommand optional>`\nOr use /setrepo <service_id> | <repo> | <branch> | <startCommand>", reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # ENV VARS
    if data.startswith("svc_env:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=service_menu(sid))
            return
        ok, res = await api.list_env_vars(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not list env vars.\n{res}", reply_markup=service_menu(sid))
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

    if data.startswith("env_add:"):
        sid = data.split(":", 1)[1]
        set_pending(uid, {"type": "env_add", "service_id": sid})
        await safe_edit(callback.message, "Send env var lines (KEY=VALUE) in private chat. Lines without '=' will be ignored.", reply_markup=service_menu(sid))
        return

    if data.startswith("env_del:"):
        sid = data.split(":", 1)[1]
        set_pending(uid, {"type": "env_del", "service_id": sid})
        await safe_edit(callback.message, "Send the ENV KEY (exact name) you want to delete in private chat.", reply_markup=service_menu(sid))
        return

    # fallback
    await safe_edit(callback.message, "Unknown action. Use /start", reply_markup=main_menu())
# ---------- private message handlers (create, pending flows, commands) ----------

@app.on_message(filters.private & filters.text)
async def private_text_handler(client: Client, message: Message):
    txt = message.text.strip()
    uid = message.from_user.id

    # Quick commands: /create, /setrepo, /login, /logs
    if txt.startswith("/create "):
        parts = [p.strip() for p in txt[len("/create "):].split("|")]
        if len(parts) < 1:
            await safe_reply(message, "Usage: /create <NAME> | <GIT_REPO optional> | <branch optional> | <startCommand optional>")
            return
        name = parts[0]
        repo = parts[1] if len(parts) >= 2 and parts[1] else None
        branch = parts[2] if len(parts) >= 3 and parts[2] else "main"
        start_cmd = parts[3] if len(parts) >= 4 and parts[3] else None

        # check pending for service_type
        pending = pop_pending(uid)
        service_type = None
        if pending and pending.get("type") == "create":
            service_type = pending.get("service_type")
        if not service_type:
            await safe_reply(message, "Please first select service type with the inline menu (/start -> Create App) or provide type in the command.")
            return
        api_key = get_user_key(uid)
        if not api_key:
            await safe_reply(message, "Please /login first with /login <RENDER_API_KEY>")
            return
        api = RenderAPI(api_key)
        owner_id, raw = await api.resolve_owner_id()
        if not owner_id:
            await safe_reply(message, f"❌ Could not determine ownerId. Raw response: {raw}")
            return
        await safe_reply(message, "🛠 Creating service...")
        ok, res = await api.create_service(owner_id=owner_id, name=name, service_type=service_type,
                                           repo=repo, branch=branch, start_command=start_cmd)
        if ok:
            sid = res.get("id") or (res.get("service") or {}).get("id")
            if sid:
                set_repo_mapping(sid, repo or "", branch, start_cmd)
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
        sid = parts[0]; repo = parts[1]
        branch = parts[2] if len(parts) >= 3 and parts[2] else "main"
        start_cmd = parts[3] if len(parts) >= 4 and parts[3] else None
        api_key = get_user_key(uid)
        if not api_key:
            await safe_reply(message, "Please /login first.")
            return
        api = RenderAPI(api_key)
        update_fields = {}
        if repo:
            update_fields["repo"] = repo
        if branch:
            update_fields["branch"] = branch
        if start_cmd:
            update_fields["startCommand"] = start_cmd
        if not update_fields:
            await safe_reply(message, "Nothing to update.")
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
            api_key = parts[1].strip()
            api = RenderAPI(api_key)
            ok, owners = await api.owners()
            if not ok:
                await safe_reply(message, f"❌ Invalid key or API unreachable.\n{owners}")
                return
            set_user_key(uid, api_key)
            await safe_reply(message, "✅ API key saved. Use inline menu or /create.")
        return

    if txt.startswith("/logs "):
        parts = txt.split(maxsplit=1)
        if len(parts) < 2:
            await safe_reply(message, "Usage: /logs <service_id>")
            return
        sid = parts[1].strip()
        api_key = get_user_key(uid)
        if not api_key:
            await safe_reply(message, "Please /login first.")
            return
        api = RenderAPI(api_key)
        ok, logs_resp = await api.get_service_logs(sid, tail=True, limit=200)
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
            text = text[-3900:]; text = "(truncated)\n" + text
        await safe_reply(message, "<b>Logs</b>\n<pre>" + html_escape(text) + "</pre>", parse_mode=ParseMode.HTML)
        return

    # handle pending flows
    pending = pop_pending(uid)
    if pending:
        typ = pending.get("type")
        if typ == "set_repo":
            sid = pending.get("service_id")
            parts = [p.strip() for p in txt.split("|")]
            repo = parts[0] if parts else None
            branch = parts[1] if len(parts) >= 2 and parts[1] else "main"
            start_cmd = parts[2] if len(parts) >= 3 and parts[2] else None
            api_key = get_user_key(uid)
            if not api_key:
                await safe_reply(message, "Please /login first.")
                return
            api = RenderAPI(api_key)
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
            api_key = get_user_key(uid)
            if not api_key:
                await safe_reply(message, "Please /login first.")
                return
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
            api = RenderAPI(api_key)
            ok, res = await api.upsert_env_vars(sid, kv)
            if ok:
                await safe_reply(message, "✅ Env vars upserted.")
            else:
                await safe_reply(message, f"❌ Failed to upsert env vars.\n{res}")
            return

        if typ == "env_del":
            sid = pending.get("service_id")
            key_name = txt.strip()
            if not key_name:
                await safe_reply(message, "No key provided. Cancelled.")
                return
            api_key = get_user_key(uid)
            if not api_key:
                await safe_reply(message, "Please /login first.")
                return
            api = RenderAPI(api_key)
            ok, res = await api.delete_env_var(sid, key_name)
            if ok:
                await safe_reply(message, f"✅ Env var <b>{html_escape(key_name)}</b> deleted.", parse_mode=ParseMode.HTML)
            else:
                await safe_reply(message, f"❌ Delete failed.\n{res}")
            return

    # fallback
    await safe_reply(message, "Unrecognized input. Use /help or inline menu.")


# ---------- debug / helpers ----------
@app.on_message(filters.command("dumpstate"))
async def cmd_dumpstate(_, message: Message):
    await message.reply_text(f"State keys: {list(state.keys())}\n\n{json.dumps(state, indent=2)[:3500]}")


# ---------- run ----------
if __name__ == "__main__":
    print("🤖 Render Manager Bot starting...")
    app.run()
