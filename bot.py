# bot.py
import os
import json
from typing import Dict, Any
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from render_api import RenderAPI

# ========= CONFIG =========
BOT_TOKEN = "8298721017:AAHquRSfWT5fk9DnN0clpH84jT6UTjeoBmc"
API_ID = 23907288
API_HASH = "f9a47570ed19aebf8eb0f0a5ec1111e5"
DATA_FILE = "state.json"  # persists user keys & repo mappings
# ==========================

app = Client("render_manager_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Persisted state: {"api_keys": {user_id: key}, "repos": {service_id: {"repo": "...", "branch": "..."}}}
state = {"api_keys": {}, "repos": {}}

def load_state():
    global state
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {"api_keys": {}, "repos": {}}

def save_state():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

load_state()

def api_for(user_id: int) -> RenderAPI:
    key = state["api_keys"].get(str(user_id))
    return RenderAPI(key) if key else None

# ------------- UI Helpers -------------
def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Account", callback_data="account")],
            [
                InlineKeyboardButton("📋 List Apps", callback_data="list_apps"),
                InlineKeyboardButton("🚀 New Deploy", callback_data="deploy_menu_root"),
            ],
            [
                InlineKeyboardButton("🌐 Env Vars", callback_data="env_menu_root"),
                InlineKeyboardButton("🪵 Logs", callback_data="logs_menu_root"),
            ],
            [InlineKeyboardButton("❓ Help", callback_data="help")],
        ]
    )

def service_inline(svc: Dict[str, Any]):
    name = svc.get("service", {}).get("name") or svc.get("name") or "unknown"
    sid = svc.get("service", {}).get("id") or svc.get("id")
    return InlineKeyboardButton(f"📱 {name}", callback_data=f"svc:{sid}")

def svc_menu(service_id: str):
    rows = [
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
    return InlineKeyboardMarkup(rows)

# ------------- Commands -------------
@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    await m.reply_text(
        "Welcome to **Render Manager Bot** 🤖\n\n"
        "Use `/login <RENDER_API_KEY>` to connect your account.\nThen press **Menu**.",
        reply_markup=main_menu(),
        disable_web_page_preview=True
    )

@app.on_message(filters.command("login"))
async def login_cmd(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/login <RENDER_API_KEY>`", quote=True)
    api_key = m.command[1].strip()
    test = RenderAPI(api_key)
    ok, _ = test.test_key()
    if not ok:
        return await m.reply_text("❌ Invalid API key or API unreachable.", quote=True)
    state["api_keys"][str(m.from_user.id)] = api_key
    save_state()
    await m.reply_text("✅ API key saved!\nOpen Menu below.", reply_markup=main_menu())

@app.on_message(filters.command("menu"))
async def menu_cmd(_, m: Message):
    await m.reply_text("Main Menu ⚙️", reply_markup=main_menu())

# ------------- Callbacks -------------
@app.on_callback_query()
async def on_cb(_, q: CallbackQuery):
    uid = q.from_user.id
    api = api_for(uid)
    data = q.data or ""

    if data == "help":
        return await q.message.edit_text(
            "**Help**\n"
            "• /login <key> – connect Render\n"
            "• Use buttons to navigate: Account, Apps, Deploy, Env Vars, Logs\n"
            "• Inside a Service: Status, Restart, Delete, Env Vars, Repo, Deploy\n",
            reply_markup=main_menu()
        )

    if not api:
        return await q.message.edit_text("Please `/login <RENDER_API_KEY>` first.", reply_markup=main_menu())

    # Account
    if data == "account":
        ok, me = api.owner()
        if not ok:
            return await q.message.edit_text(f"❌ Could not fetch account.\n{me}", reply_markup=main_menu())
        msg = (
            "👤 **Account**\n"
            f"ID: `{me.get('id','-')}`\n"
            f"Name: `{me.get('name','-')}`\n"
            f"Email: `{me.get('email','-')}`"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 List Apps", callback_data="list_apps")],
                                   [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])
        return await q.message.edit_text(msg, reply_markup=kb)

    # List Apps
    if data == "list_apps":
        ok, services = api.list_services()
        if not ok:
            return await q.message.edit_text(f"❌ Failed to list services.\n{services}", reply_markup=main_menu())

        if not isinstance(services, list) or not services:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_main")]])
            return await q.message.edit_text("No services found.", reply_markup=kb)

        rows = [[service_inline(s)] for s in services]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
        return await q.message.edit_text("📋 **Your Services**", reply_markup=InlineKeyboardMarkup(rows))

    if data == "back_main":
        return await q.message.edit_text("Main Menu ⚙️", reply_markup=main_menu())

    # Service screen
    if data.startswith("svc:"):
        sid = data.split(":", 1)[1]
        ok, svc = api.get_service(sid)
        if not ok:
            return await q.message.edit_text(f"❌ Could not fetch service.\n{svc}", reply_markup=main_menu())
        sname = svc.get("service", {}).get("name", "unknown")
        stype = svc.get("service", {}).get("type", "-")
        status = (
            svc.get("service", {})
               .get("serviceDetails", {})
               .get("status", svc.get("service", {}).get("status", "-"))
        )
        text = f"**{sname}** (`{sid}`)\nType: `{stype}`\nStatus: `{status}`"
        return await q.message.edit_text(text, reply_markup=svc_menu(sid), disable_web_page_preview=True)

    # Status refresh
    if data.startswith("svc_status:"):
        sid = data.split(":", 1)[1]
        ok, svc = api.get_service(sid)
        if not ok:
            return await q.message.edit_text(f"❌ Could not fetch status.\n{svc}", reply_markup=svc_menu(sid))
        sname = svc.get("service", {}).get("name", "unknown")
        stype = svc.get("service", {}).get("type", "-")
        status = (
            svc.get("service", {})
               .get("serviceDetails", {})
               .get("status", svc.get("service", {}).get("status", "-"))
        )
        return await q.message.edit_text(f"**{sname}** (`{sid}`)\nType: `{stype}`\nStatus: `{status}`",
                                         reply_markup=svc_menu(sid))

    # Restart
    if data.startswith("svc_restart:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text("⏳ Restarting...")
        ok, res = api.restart_service(sid)
        txt = "✅ Restart triggered." if ok else f"❌ Restart failed.\n{res}"
        return await q.message.edit_text(txt, reply_markup=svc_menu(sid))

    # Delete
    if data.startswith("svc_delete:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text("⚠️ Deleting service...")
        ok, res = api.delete_service(sid)
        if ok:
            return await q.message.edit_text("🗑 Deleted successfully.", reply_markup=main_menu())
        return await q.message.edit_text(f"❌ Delete failed.\n{res}", reply_markup=svc_menu(sid))

    # Logs
    if data.startswith("svc_logs:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text("📥 Fetching logs (last 200 lines)...")
        ok, logs = api.get_logs(sid, tail_lines=200)
        if not ok:
            return await q.message.edit_text(f"❌ Logs fetch failed.\n{logs}", reply_markup=svc_menu(sid))
        text = ""
        if isinstance(logs, dict) and "logs" in logs:
            # Common shape: {"logs": [{"message": "..."} ...]}
            lines = [row.get("message", "") for row in logs.get("logs", [])]
            text = "\n".join(lines[-200:]) or "(no logs)"
        elif isinstance(logs, str):
            text = logs
        else:
            text = json.dumps(logs, indent=2)[:3500]
        text = f"🪵 **Logs**\n```\n{text[-3500:]}\n```"
        return await q.message.edit_text(text, reply_markup=svc_menu(sid), parse_mode="markdown")

    # Deploy
    if data.startswith("svc_deploy:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text("🚀 Triggering deploy...")
        ok, res = api.trigger_deploy(sid)
        if not ok:
            return await q.message.edit_text(f"❌ Deploy failed.\n{res}", reply_markup=svc_menu(sid))
        dep_id = res.get("id", "-") if isinstance(res, dict) else "-"
        return await q.message.edit_text(f"✅ Deploy triggered.\nDeploy ID: `{dep_id}`", reply_markup=svc_menu(sid))

    # Env Vars menu (per service)
    if data.startswith("svc_env:"):
        sid = data.split(":", 1)[1]
        ok, envs = api.list_env_vars(sid)
        if not ok:
            return await q.message.edit_text(f"❌ Could not list env vars.\n{envs}", reply_markup=svc_menu(sid))
        lines = []
        if isinstance(envs, dict) and "envVars" in envs:
            pairs = envs["envVars"]
        elif isinstance(envs, list):
            pairs = envs
        else:
            pairs = []
        for item in pairs:
            k = item.get("key") or item.get("name")
            v = item.get("value", "")
            lines.append(f"{k}={v}")
        text = "🌐 **Env Vars**\n" + ("(none)" if not lines else "```\n" + "\n".join(lines[:100]) + "\n```")
        kb = [
            [InlineKeyboardButton("➕ Add/Update", callback_data=f"env_add:{sid}")],
            [InlineKeyboardButton("➖ Delete", callback_data=f"env_del:{sid}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"svc:{sid}")]
        ]
        return await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="markdown")

    # Add/Update env
    if data.startswith("env_add:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text(
            "Send env var(s) like:\n`KEY=VALUE` or multiple lines:\n```\nA=1\nB=hello\n```",
            parse_mode="markdown"
        )

        @app.on_message(filters.text & filters.private)
        async def _env_add_once(_, m: Message):
            try:
                lines = [x.strip() for x in m.text.splitlines() if x.strip()]
                kv = {}
                for line in lines:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        kv[k.strip()] = v.strip()
                if not kv:
                    return await m.reply_text("No valid lines found.")
                ok, res = api_for(m.from_user.id).upsert_env_vars(sid, kv)
                txt = "✅ Env vars upserted." if ok else f"❌ Failed.\n{res}"
                await m.reply_text(txt, reply_markup=svc_menu(sid))
            except Exception as e:
                await m.reply_text(f"Error: {e}")

    # Delete env
    if data.startswith("env_del:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text("Send the ENV KEY to delete (exact name).")

        @app.on_message(filters.text & filters.private)
        async def _env_del_once(_, m: Message):
            key = m.text.strip()
            ok, res = api_for(m.from_user.id).delete_env_var(sid, key)
            txt = "✅ Deleted." if ok else f"❌ Failed.\n{res}"
            await m.reply_text(txt, reply_markup=svc_menu(sid))

    # Repo set
    if data.startswith("svc_repo_set:"):
        sid = data.split(":", 1)[1]
        await q.message.edit_text(
            "Send repo & branch like:\n`https://github.com/USER/REPO | main`\n(Branch optional; default main)"
        )

        @app.on_message(filters.text & filters.private)
        async def _repo_set_once(_, m: Message):
            raw = m.text.strip()
            if "|" in raw:
                repo, branch = [x.strip() for x in raw.split("|", 1)]
            else:
                repo, branch = raw, "main"
            ok, res = api_for(m.from_user.id).set_repo(sid, repo=repo, branch=branch)
            if ok:
                state["repos"][sid] = {"repo": repo, "branch": branch}
                save_state()
                await m.reply_text(f"✅ Repo set.\n`{repo}` @ `{branch}`", reply_markup=svc_menu(sid), parse_mode="markdown")
            else:
                await m.reply_text(f"❌ Failed to set repo.\n{res}", reply_markup=svc_menu(sid))

    # Root placeholders for future global menus
    if data in ("deploy_menu_root", "env_menu_root", "logs_menu_root"):
        return await q.message.edit_text("Choose **List Apps** → select a service to manage those actions.",
                                         reply_markup=main_menu())

# ------------- Run -------------
if __name__ == "__main__":
    print("🤖 Render Manager Bot running...")
    app.run()
