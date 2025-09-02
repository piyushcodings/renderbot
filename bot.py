# bot.py
"""
Render Manager Bot - full working version (~700 lines)
Features:
- /start, /login <API_KEY>
- Inline menus: Account, List Apps, Create App, Repo Mappings
- List Apps with safe limits
- Service menu: Status, Logs (pagination), Restart, Deploy, Set Repo/Start, Env Vars
- Create flow asks for service type, then collects details
- OwnerId resolution
- Safe HTML parsing with fallback
- State persisted in state.json (api_keys, repos, pending flows)
- Full private chat handling for /create, /setrepo, env vars
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

    # CREATE ROOT
    if data == "create_root":
        await safe_edit(callback.message, "Select a service type to create:", reply_markup=service_type_kb())
        return

    # SERVICE TYPE selected
    if data.startswith("create_type:"):
        stype = data.split(":", 1)[1]
        if stype not in VALID_SERVICE_TYPES:
            await safe_edit(callback.message, "Invalid service type selected.", reply_markup=main_menu())
            return
        set_pending(uid, {"type": "create", "service_type": stype})
        await safe_edit(callback.message,
                        f"Selected <b>{html_escape(stype)}</b>.\nSend create details in private chat using:\n"
                        "`/create <NAME> | <GIT_REPO optional> | <branch optional> | <startCommand optional>`",
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
        txt = f"<b>Service:</b> {html_escape(name)}\nID: <code>{html_escape(sid)}</code>"
        await safe_edit(callback.message, txt, reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # SERVICE STATUS
    if data.startswith("svc_status:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, status = await api.get_status(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not get status:\n{status}", reply_markup=service_menu(sid))
            return
        txt = f"<b>Status for {sid}</b>\n" + json.dumps(status, indent=2)
        await safe_edit(callback.message, txt, reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # SERVICE LOGS
    if data.startswith("svc_logs:"):
        try:
            _, sid, page = data.split(":")
            page = int(page)
        except:
            page = 1
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, logs = await api.get_logs(sid, page=page)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not get logs:\n{logs}", reply_markup=service_menu(sid))
            return
        log_text = "\n".join(logs[:100]) if isinstance(logs, list) else str(logs)
        await safe_edit(callback.message, f"<b>Logs page {page}</b>:\n<pre>{html_escape(log_text)}</pre>", reply_markup=logs_nav(sid, page), parse_mode=ParseMode.HTML)
        return

    # SERVICE RESTART
    if data.startswith("svc_restart:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        ok, res = await api.restart_service(sid)
        msg = "✅ Restarted successfully." if ok else f"❌ Failed: {res}"
        await safe_edit(callback.message, msg, reply_markup=service_menu(sid))
        return

    # SERVICE DEPLOY
    if data.startswith("svc_deploy:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu())
            return
        repo = get_repo_mapping(sid)
        if not repo:
            await safe_edit(callback.message, "No repo mapping found. Set it first.", reply_markup=service_menu(sid))
            return
        ok, res = await api.deploy_service(sid, repo.get("repo"), repo.get("branch"), repo.get("startCommand"))
        msg = "✅ Deployed successfully." if ok else f"❌ Deploy failed: {res}"
        await safe_edit(callback.message, msg, reply_markup=service_menu(sid))
        return

    # SERVICE SET REPO
    if data.startswith("svc_repo_set:"):
        sid = data.split(":", 1)[1]
        set_pending(uid, {"type": "set_repo", "service_id": sid})
        await safe_edit(callback.message, f"Send repo info in private chat using:\n"
                                          "`/setrepo <GIT_REPO> | <branch optional> | <startCommand optional>`",
                        reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return

    # SERVICE ENV
    if data.startswith("svc_env:"):
        sid = data.split(":", 1)[1]
        set_pending(uid, {"type": "env", "service_id": sid})
        await safe_edit(callback.message, f"Send env vars in private chat using:\n"
                                          "`/env <KEY1>=<VAL1> | <KEY2>=<VAL2> ...`",
                        reply_markup=service_menu(sid), parse_mode=ParseMode.HTML)
        return


# ---------- PRIVATE MESSAGE HANDLERS ----------
@app.on_message(filters.private & filters.command("create"))
async def pm_create(_, message: Message):
    pending = pop_pending(message.from_user.id)
    api_key = get_user_key(message.from_user.id)
    if not api_key:
        await safe_reply(message, "Please /login first.")
        return
    if not pending or pending.get("type") != "create":
        await safe_reply(message, "Start creation from inline menu /start → Create App.")
        return
    api = RenderAPI(api_key)
    try:
        parts = message.text.split(" ", 1)[1].split("|")
        name = parts[0].strip()
        repo = parts[1].strip() if len(parts) > 1 else None
        branch = parts[2].strip() if len(parts) > 2 else "main"
        startCommand = parts[3].strip() if len(parts) > 3 else None
    except:
        await safe_reply(message, "Invalid format. Use:\n`/create <NAME> | <GIT_REPO optional> | <branch optional> | <startCommand optional>`", parse_mode=ParseMode.HTML)
        return
    ok, svc = await api.create_service(pending["service_type"], name, repo, branch, startCommand)
    if ok:
        sid = svc.get("id") if isinstance(svc, dict) else str(svc)
        await safe_reply(message, f"✅ Created service <b>{html_escape(name)}</b> with ID <code>{sid}</code>", parse_mode=ParseMode.HTML)
    else:
        await safe_reply(message, f"❌ Failed to create: {svc}")


@app.on_message(filters.private & filters.command("setrepo"))
async def pm_setrepo(_, message: Message):
    pending = pop_pending(message.from_user.id)
    if not pending or pending.get("type") != "set_repo":
        await safe_reply(message, "Start from inline menu → Set Repo/Start")
        return
    sid = pending["service_id"]
    try:
        parts = message.text.split(" ", 1)[1].split("|")
        repo = parts[0].strip()
        branch = parts[1].strip() if len(parts) > 1 else "main"
        startCommand = parts[2].strip() if len(parts) > 2 else None
        set_repo_mapping(sid, repo, branch, startCommand)
        await safe_reply(message, f"✅ Repo mapping saved for {sid}.")
    except:
        await safe_reply(message, "Invalid format. Use:\n`/setrepo <GIT_REPO> | <branch optional> | <startCommand optional>`", parse_mode=ParseMode.HTML)


@app.on_message(filters.private & filters.command("env"))
async def pm_env(_, message: Message):
    pending = pop_pending(message.from_user.id)
    if not pending or pending.get("type") != "env":
        await safe_reply(message, "Start from inline menu → Env Vars")
        return
    sid = pending["service_id"]
    api_key = get_user_key(message.from_user.id)
    if not api_key:
        await safe_reply(message, "Please /login first.")
        return
    api = RenderAPI(api_key)
    try:
        parts = message.text.split(" ", 1)[1].split("|")
        env_vars = {}
        for p in parts:
            if "=" in p:
                k, v = p.strip().split("=", 1)
                env_vars[k.strip()] = v.strip()
        ok, res = await api.set_env(sid, env_vars)
        msg = "✅ Env vars set." if ok else f"❌ Failed: {res}"
        await safe_reply(message, msg)
    except:
        await safe_reply(message, "Invalid format. Use:\n`/env <KEY1>=<VAL1> | <KEY2>=<VAL2>`", parse_mode=ParseMode.HTML)


# ---------- START ----------
if __name__ == "__main__":
    logger.info("Starting Render Manager Bot...")
    app.run()
