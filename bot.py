# bot.py
"""
Render Manager Bot (Pyrogram)
Full-featured manager with inline keyboards.

Features:
- /start, /login <RENDER_API_KEY>
- Inline menu with account, list apps
- Per-service menu: Status, Restart, Delete, Logs, Env Vars, Set Repo, Deploy
- Deploy trigger, list deploys
- Env management (list, add/update, delete)
- Set repo (repo | branch)
- Persist api keys and repo mapping in state.json
- Robust parse_mode handling for Pyrogram v2 using pyrogram.enums.ParseMode
"""

import os
import json
import html
import logging
from typing import Dict, Any, Optional

from pyrogram import Client, filters, errors
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from pyrogram.enums import ParseMode

from render_api import RenderAPI  # ensure render_api.py (wrapper) is present

# ---------------- CONFIG ----------------
# IMPORTANT: Do NOT hardcode tokens in production. Use environment variables.
BOT_TOKEN = "8298721017:AAHquRSfWT5fk9DnN0clpH84jT6UTjeoBmc"
API_ID = 23907288
API_HASH = "f9a47570ed19aebf8eb0f0a5ec1111e5"
STATE_FILE = os.getenv("STATE_FILE", "state.json")
# ----------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not API_ID or not API_HASH:
    logger.warning("BOT_TOKEN / API_ID / API_HASH not set. Make sure to provide them as environment variables.")

app = Client("render_manager", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# state: {"api_keys": {user_id: key}, "repos": {service_id: {"repo": "...", "branch": "..."}}}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {"api_keys": {}, "repos": {}}
else:
    state = {"api_keys": {}, "repos": {}}

# pending actions used for one-shot text replies (env add/delete, set repo)
pending_actions: Dict[str, Dict[str, Any]] = {}  # keyed by str(user_id)

# ---------------- state helpers ----------------
def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.exception("Failed to save state: %s", e)

def api_for(user_id: int) -> Optional[RenderAPI]:
    key = state.get("api_keys", {}).get(str(user_id))
    if not key:
        return None
    return RenderAPI(key)

# ---------------- Safe messaging helpers ----------------
# Use ParseMode enum throughout. Fallback to plain text if parse fails.
async def safe_edit(msg_obj: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """
    Try to edit message. If MessageNotModified ignore.
    If entity/parse problems occur, attempt fallback (reply without parse mode).
    """
    try:
        await msg_obj.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except errors.MessageNotModified:
        return
    except errors.RPCError as e:
        msg = str(e)
        logger.debug("safe_edit RPCError: %s", msg)
        # If parse mode or entity issues, fallback to reply_text without parse_mode
        if "ENTITY_BOUNDS_INVALID" in msg or "Invalid parse mode" in msg or "entities" in msg:
            try:
                await msg_obj.reply_text(text, reply_markup=reply_markup)
                return
            except Exception:
                logger.exception("Fallback reply_text in safe_edit failed")
        # otherwise re-raise or log
        logger.exception("Unexpected RPCError in safe_edit: %s", e)
    except Exception:
        logger.exception("Unexpected error in safe_edit")

async def safe_reply(msg_obj: Message, text: str, reply_markup=None, parse_mode=ParseMode.HTML):
    """
    Try reply_text with parse_mode; if RPC parse errors happen fallback to plain reply_text.
    """
    try:
        await msg_obj.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except errors.RPCError as e:
        msg = str(e)
        logger.debug("safe_reply RPCError: %s", msg)
        if "ENTITY_BOUNDS_INVALID" in msg or "Invalid parse mode" in msg or "entities" in msg:
            try:
                await msg_obj.reply_text(text, reply_markup=reply_markup)
                return
            except Exception:
                logger.exception("Fallback plain reply_text in safe_reply failed")
        logger.exception("RPCError in safe_reply: %s", e)
    except Exception:
        logger.exception("Unexpected in safe_reply")

# ---------------- Keyboards ----------------
def main_menu_kb():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Account", callback_data="account")],
            [InlineKeyboardButton("📋 List Apps", callback_data="list_apps")],
            [InlineKeyboardButton("🚀 Deploy (Choose App)", callback_data="deploy_root")],
            [InlineKeyboardButton("🌐 Env Vars (Choose App)", callback_data="env_root")],
            [InlineKeyboardButton("🪵 Logs (Choose App)", callback_data="logs_root")],
        ]
    )

def svc_menu_kb(service_id: str):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📡 Status", callback_data=f"svc_status:{service_id}")],
            [
                InlineKeyboardButton("🔄 Restart", callback_data=f"svc_restart:{service_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"svc_delete:{service_id}"),
            ],
            [
                InlineKeyboardButton("🪵 Logs", callback_data=f"svc_logs:{service_id}"),
                InlineKeyboardButton("🌐 Env Vars", callback_data=f"svc_env:{service_id}"),
            ],
            [
                InlineKeyboardButton("🔗 Set Repo", callback_data=f"svc_repo_set:{service_id}"),
                InlineKeyboardButton("🚀 Deploy", callback_data=f"svc_deploy:{service_id}"),
            ],
            [InlineKeyboardButton("⬅️ Back to Apps", callback_data="list_apps")],
        ]
    )

# ---------------- Commands ----------------
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    text = (
        "Welcome to <b>Render Manager Bot</b>.\n\n"
        "1. Connect with <b>/login &lt;RENDER_API_KEY&gt;</b>\n"
        "2. Open the menu below and manage your services with buttons.\n\n"
        "Use the inline buttons for quick actions."
    )
    await safe_reply(message, text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

@app.on_message(filters.command("login"))
async def cmd_login(client: Client, message: Message):
    if len(message.command) < 2:
        await safe_reply(message, "Usage: <b>/login &lt;RENDER_API_KEY&gt;</b>", parse_mode=ParseMode.HTML)
        return

    api_key = message.command[1].strip()
    api = RenderAPI(api_key)
    ok, data = api.test_key()
    if not ok:
        reason = data.get("message") if isinstance(data, dict) else data
        await safe_reply(message, f"❌ Invalid API key or API unreachable.\n{html.escape(str(reason))}", parse_mode=ParseMode.HTML)
        return

    state.setdefault("api_keys", {})[str(message.from_user.id)] = api_key
    save_state()
    await safe_reply(message, "✅ API key saved. Use the menu below.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

@app.on_message(filters.command("menu"))
async def cmd_menu(_, message: Message):
    await safe_reply(message, "Main Menu:", reply_markup=main_menu_kb())

# ---------------- Callback handler ----------------
@app.on_callback_query()
async def on_cb(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    api = api_for(user_id)
    data = callback.data or ""

    # ACCOUNT
    if data == "account":
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        ok, info = api.owner()
        if not ok:
            info_text = html.escape(str(info))[:2000]
            await safe_edit(callback.message, f"❌ Could not fetch account.\n{info_text}", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        name = html.escape(str(info.get("name", info.get("email", "-"))))
        email = html.escape(str(info.get("email", "-")))
        acc_id = html.escape(str(info.get("id", "-")))
        text = f"<b>Account</b>\nName: {name}\nEmail: {email}\nID: {acc_id}"
        await safe_edit(callback.message, text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    # LIST APPS
    if data in ("list_apps", "list"):
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        ok, svcs = api.list_services()
        if not ok:
            await safe_edit(callback.message, f"❌ Failed to list services.\n{html.escape(str(svcs))}", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        if not svcs:
            await safe_edit(callback.message, "No services found.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        rows = []
        for s in svcs:
            service = s.get("service", s) if isinstance(s, dict) else s
            sid = service.get("id") or s.get("id")
            sname = html.escape(str(service.get("name") or s.get("name") or "unknown"))
            rows.append([InlineKeyboardButton(f"📱 {sname}", callback_data=f"svc:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="account")])
        await safe_edit(callback.message, "📋 <b>Your Services</b>:", reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        return

    # ROOT ACTION INSTRUCT
    if data in ("deploy_root", "env_root", "logs_root"):
        text = "Select a service first: /menu → List Apps → choose a service to perform this action."
        await safe_edit(callback.message, text, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    # SERVICE DETAILS & MENU
    if data.startswith("svc:"):
        sid = data.split(":", 1)[1]
        if not api:
            await safe_edit(callback.message, "Please /login first.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        ok, svc = api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch service.\n{html.escape(str(svc))}", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
            return
        service = svc.get("service", svc) if isinstance(svc, dict) else svc
        sname = html.escape(str(service.get("name", "unknown")))
        stype = html.escape(str(service.get("type", "-")))
        status = service.get("serviceDetails", {}).get("status") if service.get("serviceDetails") else service.get("status", "-")
        status = html.escape(str(status))
        text = f"<b>{sname}</b>\nID: <code>{html.escape(sid)}</code>\nType: {stype}\nStatus: {status}"
        await safe_edit(callback.message, text, reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # STATUS REFRESH
    if data.startswith("svc_status:"):
        sid = data.split(":", 1)[1]
        ok, svc = api.get_service(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not fetch status.\n{html.escape(str(svc))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
            return
        service = svc.get("service", svc)
        sname = html.escape(str(service.get("name", "unknown")))
        status = service.get("serviceDetails", {}).get("status") if service.get("serviceDetails") else service.get("status", "-")
        status = html.escape(str(status))
        await safe_edit(callback.message, f"<b>{sname}</b>\nStatus: {status}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # RESTART
    if data.startswith("svc_restart:"):
        sid = data.split(":", 1)[1]
        await safe_edit(callback.message, "⏳ Restarting...", reply_markup=None, parse_mode=ParseMode.HTML)
        ok, res = api.restart_service(sid)
        if ok:
            await safe_edit(callback.message, "✅ Restart triggered.", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_edit(callback.message, f"❌ Restart failed:\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # DELETE
    if data.startswith("svc_delete:"):
        sid = data.split(":", 1)[1]
        await safe_edit(callback.message, "⚠️ Deleting service...", reply_markup=None, parse_mode=ParseMode.HTML)
        ok, res = api.delete_service(sid)
        if ok:
            await safe_edit(callback.message, "🗑 Service deleted.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        else:
            await safe_edit(callback.message, f"❌ Delete failed:\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # LOGS
    if data.startswith("svc_logs:"):
        sid = data.split(":", 1)[1]
        await safe_edit(callback.message, "📥 Fetching logs...", reply_markup=None, parse_mode=ParseMode.HTML)
        ok, logs = api.get_logs(sid, tail=200)
        if not ok:
            await safe_edit(callback.message, f"❌ Logs fetch failed.\n{html.escape(str(logs))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
            return
        text_out = ""
        if isinstance(logs, dict):
            if "logs" in logs and isinstance(logs["logs"], list):
                lines = [str(r.get("message", "")) for r in logs["logs"]]
                text_out = "\n".join(lines[-200:])
            else:
                text_out = json.dumps(logs, indent=2)
        else:
            text_out = str(logs)
        if not text_out:
            text_out = "(no logs)"
        if len(text_out) > 3900:
            text_out = text_out[-3900:]
            text_out = "(last truncated lines)\n" + text_out
        await safe_edit(callback.message, "<b>Logs</b>\n<pre>" + html.escape(text_out) + "</pre>", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # DEPLOY
    if data.startswith("svc_deploy:"):
        sid = data.split(":", 1)[1]
        await safe_edit(callback.message, "🚀 Triggering deploy...", reply_markup=None, parse_mode=ParseMode.HTML)
        ok, res = api.trigger_deploy(sid)
        if ok:
            dep_id = res.get("id", "-") if isinstance(res, dict) else "-"
            await safe_edit(callback.message, f"✅ Deploy triggered.\nDeploy ID: <code>{html.escape(str(dep_id))}</code>", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_edit(callback.message, f"❌ Deploy failed:\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # ENV VARS LIST
    if data.startswith("svc_env:"):
        sid = data.split(":", 1)[1]
        ok, envs = api.list_env_vars(sid)
        if not ok:
            await safe_edit(callback.message, f"❌ Could not list env vars.\n{html.escape(str(envs))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
            return
        pairs = []
        if isinstance(envs, dict) and "envVars" in envs:
            pairs = envs["envVars"]
        elif isinstance(envs, list):
            pairs = envs
        text_lines = []
        for item in pairs:
            k = item.get("key") or item.get("name") or item.get("keyName")
            v = item.get("value", "")
            text_lines.append(f"{html.escape(str(k))} = {html.escape(str(v))}")
        text = "<b>Env Vars</b>\n" + ("\n".join(text_lines) if text_lines else "(none)")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add/Update", callback_data=f"env_add:{sid}")],
                [InlineKeyboardButton("➖ Delete", callback_data=f"env_del:{sid}")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"svc:{sid}")],
            ]
        )
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # ADD ENV -> set pending
    if data.startswith("env_add:"):
        sid = data.split(":", 1)[1]
        pending_actions[str(callback.from_user.id)] = {"type": "env_add", "service_id": sid}
        await safe_edit(callback.message, "Send env var(s) lines like:\nKEY=VALUE\nMULTI=lines\n\n(Will upsert)", reply_markup=None, parse_mode=ParseMode.HTML)
        return

    # DELETE ENV -> set pending
    if data.startswith("env_del:"):
        sid = data.split(":", 1)[1]
        pending_actions[str(callback.from_user.id)] = {"type": "env_del", "service_id": sid}
        await safe_edit(callback.message, "Send the ENV KEY (exact name) you want to delete:", reply_markup=None, parse_mode=ParseMode.HTML)
        return

    # SET REPO -> set pending
    if data.startswith("svc_repo_set:"):
        sid = data.split(":", 1)[1]
        pending_actions[str(callback.from_user.id)] = {"type": "set_repo", "service_id": sid}
        await safe_edit(callback.message, "Send repo & branch like:\nhttps://github.com/USER/REPO | main\n(Branch optional; default main)", reply_markup=None, parse_mode=ParseMode.HTML)
        return

    # Unknown action fallback
    await safe_edit(callback.message, "Unknown action. Use /menu.", reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

# ---------------- pending actions handler ----------------
@app.on_message(filters.private & filters.text)
async def handle_pending_text(client: Client, message: Message):
    key = str(message.from_user.id)
    if key not in pending_actions:
        return  # nothing pending

    action = pending_actions.pop(key)
    save_state()
    typ = action.get("type")
    sid = action.get("service_id")
    api = api_for(message.from_user.id)
    if not api:
        await safe_reply(message, "Please /login first.", parse_mode=ParseMode.HTML)
        return

    text = message.text.strip()

    # ENV ADD
    if typ == "env_add":
        kv = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if k:
                    kv[k] = v
        if not kv:
            await safe_reply(message, "No valid KEY=VALUE lines found. Cancelled.", parse_mode=ParseMode.HTML)
            return
        ok, res = api.upsert_env_vars(sid, kv)
        if ok:
            await safe_reply(message, "✅ Env vars upserted.", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_reply(message, f"❌ Failed to upsert env vars.\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # ENV DELETE
    if typ == "env_del":
        key_name = text.strip()
        if not key_name:
            await safe_reply(message, "No key provided. Cancelled.", parse_mode=ParseMode.HTML)
            return
        ok, res = api.delete_env_var(sid, key_name)
        if ok:
            await safe_reply(message, f"✅ Env var <b>{html.escape(key_name)}</b> deleted.", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_reply(message, f"❌ Delete failed.\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # SET REPO
    if typ == "set_repo":
        if "|" in text:
            repo_raw, branch_raw = text.split("|", 1)
            repo = repo_raw.strip()
            branch = branch_raw.strip()
        else:
            repo = text
            branch = "main"
        ok, res = api.set_repo(sid, repo=repo, branch=branch)
        if ok:
            state.setdefault("repos", {})[sid] = {"repo": repo, "branch": branch}
            save_state()
            await safe_reply(message, f"✅ Repo set: <code>{html.escape(repo)}</code> @ <b>{html.escape(branch)}</b>", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        else:
            await safe_reply(message, f"❌ Failed to set repo.\n{html.escape(str(res))}", reply_markup=svc_menu_kb(sid), parse_mode=ParseMode.HTML)
        return

    # fallback
    await safe_reply(message, "Action processed or invalid input.", parse_mode=ParseMode.HTML)

# ---------------- utility commands ----------------
@app.on_message(filters.command("whoami"))
async def whoami(_, m: Message):
    api = api_for(m.from_user.id)
    if not api:
        await m.reply_text("Not logged in.")
        return
    ok, info = api.owner()
    await m.reply_text(f"Owner fetch ok={ok}\n{json.dumps(info, indent=2)[:3000]}")

# ---------------- run ----------------
if __name__ == "__main__":
    print("🤖 Render Manager Bot starting...")
    app.run()
