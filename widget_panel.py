# -*- coding: utf-8 -*-
"""Админ-панель в виде виджета Bot API 10.4 (тест). Команда /wpanel, только главные админы, только личка.

Виджет открывается со сводкой, вшитой в HTML (запрос из виджета возможен только по нажатию),
остальные вкладки подгружаются кнопками: виджет шлёт TelegramWidget.request("t:<вкладка>"),
бот отвечает answerWidgetQuery с компактным JSON до 1024 байт. Панель только читает данные.
Лимиты Telegram: HTML до 32 КБ, после zlib до 4 КБ.
"""
import asyncio
import html as _html
import json
import logging
import time
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router(name="widget_panel")
MSK = ZoneInfo("Europe/Moscow")

_api = _get_db = _is_admin = _name = _flags = _startup = None

CAT = {"complaint": "Жалобы", "appeal": "Обжалования", "question": "Вопросы", "friends": "Друзья", "general": "Общее"}
TABS = ("sum", "tk", "sec", "nm", "ac", "sys")

PAGE = """<style>
:root{--bg:#fff;--fg:#17212b;--mu:#7d8b99;--c:#f1f4f8;--a:#2a9df4;--b:#7b61ff;--ok:#2fbf71;--no:#e5534b}
@media(prefers-color-scheme:dark){:root{--bg:#17212b;--fg:#fff;--mu:#8b9bab;--c:#222e3b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}
.h{padding:14px 14px 10px;background:linear-gradient(120deg,var(--a),var(--b));color:#fff}
.h b{font-size:17px}.h span{display:block;opacity:.8;font-size:12px;margin-top:2px}
#tb{display:flex;gap:6px;overflow-x:auto;padding:10px 10px 4px}
.t{flex:none;border:0;border-radius:16px;padding:7px 12px;background:var(--c);color:var(--fg);font-size:13px}
.t.on{background:linear-gradient(120deg,var(--a),var(--b));color:#fff}
#c{padding:8px 12px;min-height:120px;transition:opacity .2s}#c.ld{opacity:.35}
.g{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.k{background:var(--c);border-radius:14px;padding:12px;animation:up .4s both}
.k b{display:block;font-size:26px}.k span{color:var(--mu);font-size:12px}
h4{margin:14px 0 6px;font-size:13px;color:var(--mu);font-weight:600}
.r{display:flex;align-items:center;gap:8px;margin:5px 0}.r i{flex:0 0 38%;font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.r u{flex:1;height:8px;border-radius:4px;background:var(--c);overflow:hidden}
.r s{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--a),var(--b));animation:gr .6s both}
.r em{flex:0 0 28px;text-align:right;font-style:normal;color:var(--mu)}
.q{padding:7px 0;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--c)}
.d{width:9px;height:9px;border-radius:50%}.ok{background:var(--ok)}.no{background:var(--no)}
.f{display:flex;justify-content:space-between;align-items:center;padding:6px 12px 14px;color:var(--mu);font-size:12px}
#rf{border:0;border-radius:12px;padding:8px 14px;background:var(--c);color:var(--fg);font-size:13px}
@keyframes up{from{opacity:0;transform:translateY(8px)}}@keyframes gr{from{width:0}}
</style>
<div class="h"><b>🛠 ToHSecrets · Админ-панель</b><span>виджет-версия, только чтение</span></div>
<div id="tb"></div><div id="c"></div>
<div class="f"><span id="ts"></span><button id="rf">↻ Обновить</button></div>
<script>
var T=[["sum","📊","Сводка"],["tk","🎫","Тикеты"],["sec","🔮","Секретки"],["nm","✅","Норматив"],["ac","🔐","Доступ"],["sys","⚙️","Система"]],cur="sum",busy=0;
function $(i){return document.getElementById(i)}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function tabs(){$("tb").innerHTML=T.map(function(t){return'<button class="t'+(t[0]==cur?" on":"")+'" data-i="'+t[0]+'">'+t[1]+" "+t[2]+"</button>"}).join("")}
function cnt(el,to){var s=null;function f(x){s=s||x;var p=Math.min(1,(x-s)/500);el.textContent=Math.round(to*p);if(p<1)requestAnimationFrame(f)}requestAnimationFrame(f)}
function draw(d){var h="";
if(d.k)h+='<div class="g">'+d.k.map(function(x,i){var n=typeof x[1]=="number";return'<div class="k" style="animation-delay:'+i*60+'ms"><b'+(n?' data-n="'+x[1]+'">0':">"+esc(x[1]))+"</b><span>"+esc(x[0])+"</span></div>"}).join("")+"</div>";
if(d.l&&d.l[1].length){var m=Math.max.apply(0,d.l[1].map(function(r){return r[1]}))||1;h+="<h4>"+esc(d.l[0])+"</h4>"+d.l[1].map(function(r,i){return'<div class="r"><i>'+esc(r[0])+'</i><u><s style="width:'+Math.round(r[1]/m*100)+"%;animation-delay:"+i*70+'ms"></s></u><em>'+r[1]+"</em></div>"}).join("")}
if(d.s)h+=d.s.map(function(r){return'<div class="q"><span class="d '+(r[1]?"ok":"no")+'"></span>'+esc(r[0])+"</div>"}).join("");
$("c").innerHTML=h;[].forEach.call(document.querySelectorAll("b[data-n]"),function(b){cnt(b,+b.getAttribute("data-n"))});$("ts").textContent=d.t||""}
function go(id){if(busy)return;busy=1;cur=id;tabs();$("c").className="ld";
TelegramWidget.request("t:"+id).then(function(a){busy=0;$("c").className="";try{draw(JSON.parse(a.result))}catch(e){$("c").textContent="Ошибка данных"}},
function(e){busy=0;$("c").className="";$("c").textContent="Ошибка: "+(e&&e.code)})}
$("tb").onclick=function(e){var i=e.target.getAttribute("data-i");if(i&&i!=cur)go(i)};
$("rf").onclick=function(){go(cur)};
tabs();draw(__D0__);
</script>"""


def setup(dp, api_raw, get_db, is_main_admin, plain_name, flags_fn, startup_fn):
    global _api, _get_db, _is_admin, _name, _flags, _startup
    _api, _get_db, _is_admin, _name, _flags, _startup = api_raw, get_db, is_main_admin, plain_name, flags_fn, startup_fn
    dp.include_router(router)


def _one(cur, sql, args=()):
    cur.execute(sql, args)
    r = cur.fetchone()
    return r[0] if r else 0


def _collect(tab: str) -> dict:
    """Синхронно собирает данные вкладки. Имена людей подставляются позже (ключ 'ids')."""
    d = {}
    with _get_db() as conn:
        with conn.cursor() as cur:
            if tab == "sum":
                pend = _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='pending'")
                act = _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='active'")
                closed = _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='closed' AND updated_at >= now() - interval '24 hours'")
                sec = _one(cur, "SELECT COUNT(*) FROM secret_posts WHERE created_at >= now() - interval '24 hours'")
                d["k"] = [["Ждут ответа", pend], ["В работе", act], ["Закрыто за 24ч", closed], ["Секреток за 24ч", sec]]
                cur.execute("""SELECT user_id, COUNT(*) FROM secret_posts WHERE created_at >= now() - interval '24 hours'
                               GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
                d["ids"] = cur.fetchall()
                d["lt"] = "Топ публикаторов за 24ч"
            elif tab == "tk":
                d["k"] = [
                    ["Ждут ответа", _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='pending'")],
                    ["В работе", _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='active'")],
                    ["Закрыто за 7 дней", _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='closed' AND updated_at >= now() - interval '7 days'")],
                    ["Отклонено за 7 дней", _one(cur, "SELECT COUNT(*) FROM tickets WHERE status='rejected' AND updated_at >= now() - interval '7 days'")],
                ]
                cur.execute("""SELECT COALESCE(category,'general'), COUNT(*) FROM tickets
                               WHERE status IN ('pending','active') GROUP BY 1 ORDER BY 2 DESC""")
                d["l"] = ["Открытые по категориям", [[CAT.get(c, c), n] for c, n in cur.fetchall()]]
            elif tab == "sec":
                d["k"] = [
                    ["За 24 часа", _one(cur, "SELECT COUNT(*) FROM secret_posts WHERE created_at >= now() - interval '24 hours'")],
                    ["За 7 дней", _one(cur, "SELECT COUNT(*) FROM secret_posts WHERE created_at >= now() - interval '7 days'")],
                    ["Всего", _one(cur, "SELECT COUNT(*) FROM secret_posts")],
                    ["Публикаторов", _one(cur, "SELECT COUNT(*) FROM secret_publishers")],
                ]
                cur.execute("""SELECT secret_type, COUNT(*) FROM secret_posts
                               WHERE created_at >= now() - interval '7 days' AND secret_type IS NOT NULL
                               GROUP BY 1 ORDER BY 2 DESC LIMIT 6""")
                d["l"] = ["Типы за 7 дней", [[str(t)[:24], n] for t, n in cur.fetchall()]]
            elif tab == "nm":
                d["k"] = [
                    ["Активных предупреждений", _one(cur, "SELECT COUNT(*) FROM norm_warns WHERE active")],
                    ["Выдано за 7 дней", _one(cur, "SELECT COUNT(*) FROM norm_warns WHERE ts >= now() - interval '7 days'")],
                    ["Отгулов ждут решения", _one(cur, "SELECT COUNT(*) FROM norm_excuse WHERE status='pending'")],
                    ["Искателей в учёте", _one(cur, "SELECT COUNT(*) FROM norm_state")],
                ]
                cur.execute("SELECT day, met, found, required FROM norm_days ORDER BY day DESC LIMIT 7")
                rows = cur.fetchall()
                d["l"] = ["Норматив по дням (найдено)", [[f"{r[0].strftime('%d.%m')} {'✅' if r[1] else '❌'} из {r[3]}", r[2] or 0] for r in rows]]
            elif tab == "ac":
                d["k"] = [
                    ["Публикаторов", _one(cur, "SELECT COUNT(*) FROM secret_publishers")],
                    ["По наборам", _one(cur, "SELECT COUNT(*) FROM recruiters")],
                    ["Главных админов", _one(cur, "SELECT COUNT(*) FROM main_admins")],
                    ["В ЧС сейчас", _one(cur, "SELECT COUNT(*) FROM bl_entries WHERE active")],
                ]
                cur.execute("SELECT COALESCE(nick, roblox_id::text), to_char(added_at, 'DD.MM') FROM bl_entries WHERE active ORDER BY added_at DESC LIMIT 5")
                d["s"] = [[f"{n} · {dt}", 0] for n, dt in cur.fetchall()]
                d["lt"] = None
    return d


async def _build(tab: str) -> dict:
    if tab == "sys":
        t0 = time.monotonic()
        await asyncio.to_thread(_collect, "ac")   # заодно замер ответа БД
        ping = int((time.monotonic() - t0) * 1000)
        fl = _flags()
        d = {"k": [["Ответ БД, мс", ping], ["Тех. перерыв", "вкл" if fl.get("maint") else "выкл"],
                   ["Публикации", "пауза" if fl.get("pause") else "идут"], ["Юзербот", "есть" if fl.get("userbot") else "нет"]]}
        d["s"] = [[name + (f": {note}" if note and not ok else ""), 1 if ok else 0] for name, (ok, note) in _startup().items()]
    else:
        d = await asyncio.to_thread(_collect, tab)
        if d.get("ids"):
            names = await asyncio.gather(*[_name(int(u)) for u, _ in d["ids"]], return_exceptions=True)
            d["l"] = [d.pop("lt"), [[(nm if isinstance(nm, str) else str(u))[:22], n] for (u, n), nm in zip(d["ids"], names)]]
            d.pop("ids")
        d.pop("lt", None)
    d["t"] = "обновлено " + datetime.now(MSK).strftime("%H:%M:%S") + " МСК"
    return d


def _pack(d: dict, limit: int = 1000) -> str:
    """JSON до limit байт: при переполнении режем списки."""
    while True:
        s = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        if len(s.encode()) <= limit:
            return s
        if d.get("l") and len(d["l"][1]) > 1:
            d["l"][1].pop()
        elif d.get("s") and len(d["s"]) > 1:
            d["s"].pop()
        else:
            return json.dumps({"k": [["Ошибка", "слишком много данных"]]}, ensure_ascii=False)


async def handle_query(wq: dict) -> bool:
    """Запрос из виджета. True, если это запрос панели (t:<вкладка>) и он обработан."""
    data = str(wq.get("data") or "")
    if not data.startswith("t:"):
        return False
    qid = wq.get("id")
    uid = (wq.get("from") or {}).get("id")
    if not qid:
        return True
    if not uid or not await _is_admin(uid):
        await _api("answerWidgetQuery", {"widget_query_id": qid, "data": json.dumps({"k": [["Доступ", "только главные админы"]]}, ensure_ascii=False)})
        return True
    tab = data[2:]
    try:
        d = await asyncio.wait_for(_build(tab if tab in TABS else "sum"), timeout=10)
        payload = _pack(d)
    except Exception as e:
        logging.warning(f"Виджет-панель, вкладка {tab}: {e}")
        payload = json.dumps({"k": [["Ошибка", type(e).__name__]]}, ensure_ascii=False)
    ok, res = await _api("answerWidgetQuery", {"widget_query_id": qid, "data": payload})
    if not ok:
        logging.warning(f"answerWidgetQuery (панель): {res}")
    return True


@router.message(Command("wpanel"))
async def cmd_wpanel(message: Message):
    if message.chat.type != "private":
        if await _is_admin(message.from_user.id):
            await message.reply("Виджеты в продакшене работают только в личке с ботом. Напишите /wpanel мне в ЛС.")
        return
    if not await _is_admin(message.from_user.id):
        return
    first = _pack(await _build("sum"))
    page = PAGE.replace("__D0__", first.replace("</", "<\\/"))
    size = len(zlib.compress(page.encode(), 9))
    ok, res = await _api("sendWidget", {"chat_id": message.chat.id, "html": page, "caption": "🛠 Админ-панель (виджет)",
                                        "allow_queries": True})
    if ok:
        await message.answer(f"✅ Панель отправлена (HTML {len(page.encode())} Б, сжато {size} Б из 4096).")
    else:
        await message.answer(f"❌ sendWidget не прошёл (сжато {size} Б):\n<code>{_html.escape(str(res))[:500]}</code>", parse_mode="HTML")
