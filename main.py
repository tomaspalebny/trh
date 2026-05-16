import asyncio
import html
import json
import logging
import random
import re
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import Dict, List
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trzni_arena")

app = FastAPI(title="Tržní Aréna v2 - Multi-room")

# ============ INPUT SANITIZATION & VALIDATION ============
MAX_PLAYER_NAME_LENGTH = 30
MAX_ROOM_ID_LENGTH = 20

def sanitize_name(name: str) -> str:
    """Sanitize player name to prevent XSS and enforce length limits."""
    if not name:
        return "Anonym"
    # Remove characters that are unsafe for URL paths (keep letters, digits, spaces, hyphens, underscores)
    name = re.sub(r'[^A-Za-z0-9 _-]', '', name)
    name = name.strip()[:MAX_PLAYER_NAME_LENGTH]
    sanitized = html.escape(name)
    return sanitized if sanitized else "Anonym"

def validate_room_id(room_id: str) -> bool:
    """Validate room ID format."""
    if not room_id or len(room_id) > MAX_ROOM_ID_LENGTH:
        return False
    allowed_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
    return all(c in allowed_chars for c in room_id)

async def send_error(ws: WebSocket, msg: str):
    try:
        await ws.send_json({"type": "error", "msg": msg})
    except Exception:
        pass

# ============ GAME STATE PER ROOM ============
class GameState:
    def __init__(self, room_id: str):
        self.room_id = room_id
        self.lock = asyncio.Lock()
        self.reset()

    def reset(self):
        self.players: Dict[str, dict] = {}
        self.round = 0
        self.max_rounds = 5
        self.phase = "lobby"
        self.trades: List[dict] = []
        self.round_trades: List[dict] = []
        self.offers: List[dict] = []
        self.bids: List[dict] = []
        self.tax = 0
        self.price_floor = 0
        self.price_ceiling = 999
        self.round_history = []
        self.equilibrium_price = 0
        self.equilibrium_qty = 0
        self.lock = asyncio.Lock()

    async def add_player(self, name: str) -> dict:
        async with self.lock:
            if name in self.players:
                return self.players[name]
            role = "buyer" if len(self.players) % 2 == 0 else "seller"
            if role == "buyer":
                wtp = random.randint(30, 100)
                player = {"name": name, "role": role, "wtp": wtp, "mc": None, "profit": 0, "traded_this_round": False}
            else:
                mc = random.randint(10, 70)
                player = {"name": name, "role": role, "wtp": None, "mc": mc, "profit": 0, "traded_this_round": False}
            self.players[name] = player
            self._calc_equilibrium()
            return player

    def _calc_equilibrium(self):
        self.equilibrium_price = 0
        self.equilibrium_qty = 0
        buyers = sorted([p["wtp"] for p in self.players.values() if p["role"] == "buyer"], reverse=True)
        sellers = sorted([p["mc"] for p in self.players.values() if p["role"] == "seller"])
        for i in range(min(len(buyers), len(sellers))):
            if buyers[i] >= sellers[i] + self.tax:
                self.equilibrium_qty = i + 1
                self.equilibrium_price = (buyers[i] + sellers[i] + self.tax) / 2
            else:
                break

    async def start_round(self):
        async with self.lock:
            if self.round >= self.max_rounds:
                return
            self.round += 1
            self.phase = "trading"
            self.round_trades = []
            self.offers = []
            self.bids = []
            for p in self.players.values():
                p["traded_this_round"] = False

    async def try_match(self):
        async with self.lock:
            matched = []
            self.bids.sort(key=lambda x: x["price"], reverse=True)
            self.offers.sort(key=lambda x: x["price"])
            for bid in self.bids:
                if bid["matched"]:
                    continue
                buyer = self.players.get(bid["player"])
                if buyer is None or buyer.get("traded_this_round"):
                    continue
                for offer in self.offers:
                    if offer["matched"]:
                        continue
                    seller = self.players.get(offer["player"])
                    if seller is None or seller.get("traded_this_round"):
                        continue
                    effective_price = offer["price"] + self.tax
                    if bid["price"] >= effective_price and offer["price"] >= self.price_floor and bid["price"] <= self.price_ceiling:
                        trade_price = (bid["price"] + offer["price"]) / 2
                        if trade_price < self.price_floor or trade_price > self.price_ceiling:
                            continue
                        bid["matched"] = True
                        offer["matched"] = True
                        buyer = self.players[bid["player"]]
                        seller = self.players[offer["player"]]
                        buyer_profit = buyer["wtp"] - trade_price - (self.tax / 2)
                        seller_profit = trade_price - seller["mc"] - (self.tax / 2)
                        buyer["profit"] += round(buyer_profit, 1)
                        seller["profit"] += round(seller_profit, 1)
                        buyer["traded_this_round"] = True
                        seller["traded_this_round"] = True
                        trade = {"buyer": bid["player"], "seller": offer["player"], "price": round(trade_price, 1), "round": self.round}
                        self.trades.append(trade)
                        self.round_trades.append(trade)
                        matched.append(trade)
            return matched

    async def end_round(self):
        async with self.lock:
            if self.phase != "trading":
                return
            self.phase = "results"
            prices = [t["price"] for t in self.round_trades]
            self.round_history.append({
                "round": self.round,
                "trades": len(self.round_trades),
                "avg_price": round(sum(prices)/len(prices), 1) if prices else 0,
                "eq_price": round(self.equilibrium_price, 1),
                "eq_qty": self.equilibrium_qty,
                "tax": self.tax,
                "floor": self.price_floor,
                "ceiling": self.price_ceiling
            })

# Room management
rooms: Dict[str, GameState] = {}
room_connections: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {player_name: ws}
room_admin_connections: Dict[str, List[WebSocket]] = {}  # room_id -> [ws]

def get_room(room_id: str) -> GameState:
    if room_id not in rooms:
        rooms[room_id] = GameState(room_id)
        room_connections[room_id] = {}
        room_admin_connections[room_id] = []
    return rooms[room_id]

async def broadcast_room(room_id: str, msg: dict):
    for ws in list(room_connections.get(room_id, {}).values()):
        try:
            await ws.send_json(msg)
        except Exception:
            pass
    for ws in list(room_admin_connections.get(room_id, [])):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

async def broadcast_state(room_id: str):
    game = get_room(room_id)
    leaderboard = sorted(game.players.values(), key=lambda x: x["profit"], reverse=True)
    await broadcast_room(room_id, {
        "type": "state",
        "phase": game.phase,
        "round": game.round,
        "max_rounds": game.max_rounds,
        "players": len(game.players),
        "trades": len(game.round_trades),
        "room": room_id,
        "leaderboard": [{"name": p["name"], "profit": p["profit"], "role": p["role"]} for p in leaderboard[:10]],
        "round_history": game.round_history,
        "tax": game.tax,
        "price_floor": game.price_floor,
        "price_ceiling": game.price_ceiling if game.price_ceiling < 999 else 0,
        "eq_price": round(game.equilibrium_price, 1),
        "eq_qty": game.equilibrium_qty,
        "recent_trades": [{"price": t["price"], "buyer": t["buyer"], "seller": t["seller"]} for t in game.round_trades[-5:]]
    })

# ============ ROUTES ============

@app.get("/")
async def landing_page():
    return HTMLResponse(LANDING_HTML)

@app.get("/play/{room_id}")
async def player_page(room_id: str):
    return HTMLResponse(PLAYER_HTML.replace("__ROOM_ID__", room_id))

@app.get("/admin/{room_id}")
async def admin_page(room_id: str):
    return HTMLResponse(ADMIN_HTML.replace("__ROOM_ID__", room_id))

@app.websocket("/ws/player/{room_id}/{name}")
async def player_ws(websocket: WebSocket, room_id: str, name: str):
    await websocket.accept()
    # Validate room_id
    if not validate_room_id(room_id):
        await send_error(websocket, "Neplatný kód semináře!")
        await websocket.close()
        return
    # Sanitize player name
    name = sanitize_name(name)
    game = get_room(room_id)
    player = await game.add_player(name)
    # Close old connection if same player name reconnects
    if name in room_connections.get(room_id, {}):
        try:
            await room_connections[room_id][name].close()
        except Exception:
            pass
    # Remove any pending bids/offers from previous connection
    async with game.lock:
        game.bids = [b for b in game.bids if b["player"] != name]
        game.offers = [o for o in game.offers if o["player"] != name]
    room_connections[room_id][name] = websocket
    await websocket.send_json({"type": "welcome", "player": player, "room": room_id})
    await broadcast_state(room_id)
    try:
        while True:
            try:
                data = json.loads(await websocket.receive_text())
            except (json.JSONDecodeError, KeyError):
                await send_error(websocket, "Neplatná zpráva.")
                continue
            if data.get("action") == "bid" and game.phase == "trading":
                async with game.lock:
                    p = game.players.get(name)
                    if p is None:
                        await send_error(websocket, "Hráč nenalezen.")
                        continue
                    if p["traded_this_round"]:
                        await send_error(websocket, "Už jsi v tomto kole obchodoval/a!")
                        continue
                    try:
                        price = float(data["price"])
                    except (ValueError, TypeError):
                        await send_error(websocket, "Cena musí být číslo!")
                        continue
                    if price <= 0:
                        await send_error(websocket, "Cena musí být kladná!")
                        continue
                    if p["role"] == "buyer":
                        if price > p["wtp"]:
                            await send_error(websocket, f"Nemůžeš nabídnout víc než tvá WTP ({p['wtp']})!")
                            continue
                        # Replace any previous unmatched bid from this player
                        game.bids = [b for b in game.bids if b["player"] != name]
                        game.bids.append({"player": name, "price": price, "matched": False})
                    else:
                        if price < p["mc"]:
                            await send_error(websocket, f"Nemůžeš prodávat pod svými MC ({p['mc']})!")
                            continue
                        # Replace any previous unmatched offer from this player
                        game.offers = [o for o in game.offers if o["player"] != name]
                        game.offers.append({"player": name, "price": price, "matched": False})
                    matched = await game.try_match()
                if matched:
                    await broadcast_room(room_id, {"type": "trade", "trades": matched})
                await broadcast_state(room_id)
    except WebSocketDisconnect:
        logger.info(f"Player {name} disconnected from room {room_id}")
        async with game.lock:
            game.bids = [b for b in game.bids if b["player"] != name]
            game.offers = [o for o in game.offers if o["player"] != name]
        if name in room_connections.get(room_id, {}):
            del room_connections[room_id][name]

@app.websocket("/ws/admin/{room_id}")
async def admin_ws(websocket: WebSocket, room_id: str):
    await websocket.accept()
    # Validate room_id
    if not validate_room_id(room_id):
        await send_error(websocket, "Neplatný kód semináře!")
        await websocket.close()
        return
    game = get_room(room_id)
    room_admin_connections[room_id].append(websocket)
    await broadcast_state(room_id)
    try:
        while True:
            try:
                data = json.loads(await websocket.receive_text())
            except (json.JSONDecodeError, KeyError):
                await send_error(websocket, "Neplatná zpráva.")
                continue
            action = data.get("action")
            if action == "start_round":
                async with game.lock:
                    if game.round >= game.max_rounds:
                        await send_error(websocket, "Maximální počet kol již byl dosažen.")
                        continue
                await game.start_round()
                await broadcast_room(room_id, {"type": "round_start", "round": game.round})
                await broadcast_state(room_id)
            elif action == "end_round":
                await game.end_round()
                await broadcast_room(room_id, {"type": "round_end", "round": game.round})
                await broadcast_state(room_id)
            elif action == "set_tax":
                try:
                    val = float(data["value"])
                except (ValueError, TypeError):
                    await send_error(websocket, "Daň musí být číslo.")
                    continue
                if val < 0:
                    await send_error(websocket, "Daň nemůže být záporná.")
                    continue
                async with game.lock:
                    game.tax = val
                    game._calc_equilibrium()
                await broadcast_room(room_id, {"type": "shock", "msg": f"🔔 Nová daň: {game.tax} Kč na jednotku!"})
                await broadcast_state(room_id)
            elif action == "set_floor":
                try:
                    val = float(data["value"])
                except (ValueError, TypeError):
                    await send_error(websocket, "Cenová podlaha musí být číslo.")
                    continue
                if val < 0:
                    await send_error(websocket, "Cenová podlaha nemůže být záporná.")
                    continue
                async with game.lock:
                    if game.price_ceiling < 999 and val > game.price_ceiling:
                        await send_error(websocket, f"Cenová podlaha ({val}) nemůže být vyšší než cenový strop ({game.price_ceiling})!")
                        continue
                    game.price_floor = val
                await broadcast_room(room_id, {"type": "shock", "msg": f"🔔 Cenová podlaha: {game.price_floor} Kč!"})
                await broadcast_state(room_id)
            elif action == "set_ceiling":
                try:
                    val = float(data["value"])
                except (ValueError, TypeError):
                    await send_error(websocket, "Cenový strop musí být číslo.")
                    continue
                if val <= 0:
                    await send_error(websocket, "Cenový strop musí být kladné číslo. Pro zrušení použijte tlačítko Zrušit strop.")
                    continue
                async with game.lock:
                    if game.price_floor > 0 and val < game.price_floor:
                        await send_error(websocket, f"Cenový strop ({val}) nemůže být nižší než cenová podlaha ({game.price_floor})!")
                        continue
                    game.price_ceiling = val
                await broadcast_room(room_id, {"type": "shock", "msg": f"🔔 Cenový strop: {val} Kč!"})
                await broadcast_state(room_id)
            elif action == "remove_ceiling":
                async with game.lock:
                    game.price_ceiling = 999
                await broadcast_room(room_id, {"type": "shock", "msg": "🔔 Cenový strop zrušen!"})
                await broadcast_state(room_id)
            elif action == "reset":
                async with game.lock:
                    game.reset()
                await broadcast_room(room_id, {"type": "reset"})
                await broadcast_state(room_id)
            else:
                await send_error(websocket, f"Neznámá akce: {action}")
    except WebSocketDisconnect:
        logger.info(f"Admin disconnected from room {room_id}")
        if websocket in room_admin_connections.get(room_id, []):
            room_admin_connections[room_id].remove(websocket)

# ============ LANDING PAGE ============
LANDING_HTML = """<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tržní Aréna</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.container{max-width:440px;width:100%;padding:20px}
h1{text-align:center;font-size:2.2em;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
.subtitle{text-align:center;color:#94a3b8;margin-bottom:32px;font-size:1.1em}
.card{background:#1e293b;border-radius:16px;padding:24px;margin-bottom:16px;border:1px solid #334155}
.card h2{font-size:1em;color:#94a3b8;margin-bottom:16px;text-align:center}
input,button{width:100%;padding:14px;border-radius:12px;border:none;font-size:1em;margin-bottom:10px}
input{background:#0f172a;color:#e2e8f0;border:1px solid #334155;text-align:center;font-size:1.3em;letter-spacing:4px}
input:focus{outline:none;border-color:#3b82f6}
input.name-input{letter-spacing:normal;font-size:1em}
.btn{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;font-weight:bold;cursor:pointer;transition:transform 0.1s;font-size:1.1em}
.btn:active{transform:scale(0.97)}
.btn-admin{background:linear-gradient(135deg,#f59e0b,#ef4444);font-size:0.95em}
.divider{text-align:center;color:#475569;margin:16px 0;font-size:0.85em}
.examples{text-align:center;color:#64748b;font-size:0.8em;margin-top:4px}
</style></head><body>
<div class="container">
<h1>👋 Čau, Tržní Aréna</h1>
<p class="subtitle">Mikroekonomie 1 – Simulace trhu</p>

<div class="card">
<h2>🎮 Vstup do hry</h2>
<input id="room-input" placeholder="Kód semináře" maxlength="10" autofocus>
<p class="examples">např. S01, S02, PATOK3...</p>
<input id="name-input" class="name-input" placeholder="Tvoje jméno / přezdívka" maxlength="20">
<button class="btn" onclick="joinGame()">Vstoupit do arény</button>
</div>

<div class="card">
<h2>🎓 Panel lektora</h2>
<input id="admin-room-input" placeholder="Kód semináře" maxlength="10">
<button class="btn btn-admin" onclick="openAdmin()">Otevřít admin panel</button>
</div>
</div>

<script>
function joinGame(){
  const room=document.getElementById("room-input").value.trim().toUpperCase();
  const name=document.getElementById("name-input").value.trim();
  if(!room){alert("Zadej kód semináře!");return}
  if(!name){alert("Zadej své jméno!");return}
  window.location.href="/play/"+encodeURIComponent(room)+"?name="+encodeURIComponent(name);
}
function openAdmin(){
  const room=document.getElementById("admin-room-input").value.trim().toUpperCase();
  if(!room){alert("Zadej kód semináře!");return}
  window.location.href="/admin/"+encodeURIComponent(room);
}
document.getElementById("name-input").addEventListener("keypress",e=>{if(e.key==="Enter")joinGame()});
document.getElementById("admin-room-input").addEventListener("keypress",e=>{if(e.key==="Enter")openAdmin()});
</script></body></html>"""

# ============ PLAYER HTML ============
PLAYER_HTML = """<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tržní Aréna – Hra</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{max-width:500px;margin:0 auto;padding:16px}
h1{text-align:center;font-size:1.6em;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:2px}
.room-badge{text-align:center;color:#fcd34d;font-size:1em;font-weight:bold;margin-bottom:16px}
.card{background:#1e293b;border-radius:16px;padding:20px;margin-bottom:16px;border:1px solid #334155}
.card h2{font-size:1.1em;color:#94a3b8;margin-bottom:12px}
input,button{width:100%;padding:14px;border-radius:12px;border:none;font-size:1em;margin-bottom:8px}
input{background:#0f172a;color:#e2e8f0;border:1px solid #334155}
input:focus{outline:none;border-color:#3b82f6}
.btn{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;font-weight:bold;cursor:pointer;transition:transform 0.1s}
.btn:active{transform:scale(0.97)}
.btn:disabled{opacity:0.5}
.role-tag{display:inline-block;padding:4px 12px;border-radius:20px;font-size:0.85em;font-weight:bold}
.buyer{background:#064e3b;color:#34d399}
.seller{background:#7c2d12;color:#fb923c}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.info-box{background:#0f172a;border-radius:12px;padding:12px;text-align:center}
.info-box .val{font-size:1.5em;font-weight:bold;color:#3b82f6}
.info-box .lbl{font-size:0.75em;color:#64748b}
.trade-item{padding:8px 12px;background:#0f172a;border-radius:8px;margin-bottom:4px;font-size:0.9em;display:flex;justify-content:space-between}
.msg{padding:10px;border-radius:8px;margin-bottom:8px;text-align:center;font-weight:bold}
.msg-error{background:#7f1d1d;color:#fca5a5}
.msg-success{background:#064e3b;color:#34d399}
.msg-shock{background:#78350f;color:#fcd34d}
.leaderboard-item{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1e293b}
.phase-badge{text-align:center;padding:6px 16px;border-radius:20px;font-weight:bold;display:inline-block;margin-bottom:12px}
.phase-lobby{background:#1e3a5f;color:#60a5fa}
.phase-trading{background:#064e3b;color:#34d399}
.phase-results{background:#78350f;color:#fcd34d}
.shock-banner{background:linear-gradient(135deg,#b45309,#92400e);padding:10px;border-radius:12px;text-align:center;margin-bottom:12px;font-weight:bold;color:#fcd34d}
</style></head><body>
<div class="container">
<h1>👋 Čau, Tržní Aréna</h1>
<div class="room-badge">Seminář: __ROOM_ID__</div>

<div class="card" id="status-card">
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
<span id="role-tag" class="role-tag"></span>
<span id="phase-badge" class="phase-badge"></span>
</div>
<div class="info-grid">
<div class="info-box"><div class="val" id="my-value">-</div><div class="lbl" id="value-label">Hodnota</div></div>
<div class="info-box"><div class="val" id="my-profit">0</div><div class="lbl">Můj zisk 💰</div></div>
<div class="info-box"><div class="val" id="round-num">0</div><div class="lbl">Kolo</div></div>
<div class="info-box"><div class="val" id="player-count">0</div><div class="lbl">Hráčů</div></div>
</div>
</div>

<div id="shock-banner" class="shock-banner" style="display:none"></div>
<div id="messages"></div>

<div class="card" id="trade-card">
<h2>📊 Obchodování</h2>
<div style="display:flex;gap:8px">
<input id="price-input" type="number" placeholder="Zadej cenu..." style="margin:0">
<button class="btn" style="width:auto;padding:14px 24px;margin:0" onclick="submitBid()" id="bid-btn">Odeslat</button>
</div>
<p id="trade-hint" style="color:#64748b;font-size:0.8em;margin-top:8px"></p>
</div>

<div class="card">
<h2>📈 Poslední obchody</h2>
<div id="recent-trades"><p style="color:#64748b;font-size:0.9em">Zatím žádné obchody</p></div>
</div>

<div class="card">
<h2>🏆 Žebříček</h2>
<div id="leaderboard"></div>
</div>
</div>

<script>
const ROOM="__ROOM_ID__";
const params=new URLSearchParams(window.location.search);
const NAME=params.get("name")||"Anonym";
let ws,player,gamePhase="lobby";

function connect(){
  ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/player/${encodeURIComponent(ROOM)}/${encodeURIComponent(NAME)}`);
  ws.onmessage=e=>{const d=JSON.parse(e.data);handleMsg(d)};
  ws.onclose=()=>{showMsg("Odpojeno – pokouším se znovu připojit...","error");setTimeout(connect,2000)};
}
connect();

function handleMsg(d){
  if(d.type==="welcome"){
    player=d.player;
    const rt=document.getElementById("role-tag");
    rt.textContent=player.role==="buyer"?"🛒 Kupující":"🏭 Prodávající";
    rt.className="role-tag "+(player.role==="buyer"?"buyer":"seller");
    document.getElementById("my-value").textContent=player.role==="buyer"?player.wtp:player.mc;
    document.getElementById("value-label").textContent=player.role==="buyer"?"Max. WTP":"Min. MC";
    updateHint();
  }
  if(d.type==="state"){
    gamePhase=d.phase;
    document.getElementById("round-num").textContent=d.round+"/"+d.max_rounds;
    document.getElementById("player-count").textContent=d.players;
    updatePhase(d.phase);
    let lb="";
    d.leaderboard.forEach((p,i)=>{
      const medal=i===0?"🥇":i===1?"🥈":i===2?"🥉":(i+1)+".";
      const rc=p.role==="buyer"?"#34d399":"#fb923c";
      lb+=`<div class="leaderboard-item"><span>${medal} ${p.name} <span style="color:${rc};font-size:0.8em">${p.role==="buyer"?"kupující":"prodávající"}</span></span><span style="color:#3b82f6;font-weight:bold">${p.profit} Kč</span></div>`;
    });
    document.getElementById("leaderboard").innerHTML=lb||"<p style='color:#64748b'>Zatím nikdo</p>";
    let rt="";
    d.recent_trades.forEach(t=>{rt+=`<div class="trade-item"><span>${t.buyer} ↔ ${t.seller}</span><span style="color:#fcd34d;font-weight:bold">${t.price} Kč</span></div>`});
    document.getElementById("recent-trades").innerHTML=rt||"<p style='color:#64748b;font-size:0.9em'>Zatím žádné obchody</p>";
    const sb=document.getElementById("shock-banner");
    let shocks=[];
    if(d.tax>0)shocks.push("📌 Daň: "+d.tax+" Kč");
    if(d.price_floor>0)shocks.push("📌 Cenová podlaha: "+d.price_floor+" Kč");
    if(d.price_ceiling>0)shocks.push("📌 Cenový strop: "+d.price_ceiling+" Kč");
    if(shocks.length){sb.style.display="block";sb.innerHTML=shocks.join(" &nbsp;|&nbsp; ")}else{sb.style.display="none"}
    if(player){
      const me=d.leaderboard.find(p=>p.name===player.name);
      if(me)document.getElementById("my-profit").textContent=me.profit;
    }
  }
  if(d.type==="trade"){d.trades.forEach(t=>{if(t.buyer===NAME||t.seller===NAME){showMsg(`✅ Obchod za ${t.price} Kč!`,"success")}})}
  if(d.type==="shock"){showMsg(d.msg,"shock")}
  if(d.type==="round_start"){showMsg(`🔔 Kolo ${d.round} začíná! Obchodujte!`,"success")}
  if(d.type==="round_end"){showMsg(`⏱️ Kolo ${d.round} skončilo!`,"shock")}
  if(d.type==="error"){showMsg(d.msg,"error")}
  if(d.type==="reset"){location.reload()}
}

function updatePhase(phase){
  const pb=document.getElementById("phase-badge");
  const tc=document.getElementById("trade-card");
  if(phase==="lobby"){pb.textContent="⏳ Čekárna";pb.className="phase-badge phase-lobby";tc.style.opacity="0.5"}
  else if(phase==="trading"){pb.textContent="🔥 Obchodování";pb.className="phase-badge phase-trading";tc.style.opacity="1"}
  else{pb.textContent="📊 Výsledky";pb.className="phase-badge phase-results";tc.style.opacity="0.5"}
  document.getElementById("bid-btn").disabled=phase!=="trading";
}

function updateHint(){
  if(!player)return;
  const h=document.getElementById("trade-hint");
  if(player.role==="buyer")h.textContent=`Zadej cenu, kterou jsi ochoten/a zaplatit (max ${player.wtp} Kč). Čím nižší cena, tím vyšší tvůj zisk!`;
  else h.textContent=`Zadej cenu, za kterou chceš prodat (min ${player.mc} Kč). Čím vyšší cena, tím vyšší tvůj zisk!`;
}

function submitBid(){
  const price=parseFloat(document.getElementById("price-input").value);
  if(isNaN(price)||price<=0)return showMsg("Zadej platnou cenu!","error");
  ws.send(JSON.stringify({action:"bid",price}));
  document.getElementById("price-input").value="";
}
document.getElementById("price-input").addEventListener("keypress",e=>{if(e.key==="Enter")submitBid()});

function showMsg(text,type){
  const d=document.getElementById("messages");
  const m=document.createElement("div");
  m.className="msg msg-"+type;
  m.textContent=text;
  d.prepend(m);
  setTimeout(()=>m.remove(),5000);
  if(d.children.length>5)d.lastChild.remove();
}
</script></body></html>"""

# ============ ADMIN HTML ============
ADMIN_HTML = """<!DOCTYPE html>
<html lang="cs"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tržní Aréna – Lektor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{max-width:800px;margin:0 auto;padding:20px}
h1{text-align:center;font-size:2em;background:linear-gradient(135deg,#f59e0b,#ef4444);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
.room-badge{text-align:center;color:#fcd34d;font-size:1.2em;font-weight:bold;margin-bottom:20px}
.card{background:#1e293b;border-radius:16px;padding:20px;margin-bottom:16px;border:1px solid #334155}
.card h2{font-size:1.1em;color:#94a3b8;margin-bottom:12px}
.btn{padding:12px 24px;border-radius:12px;border:none;font-size:1em;font-weight:bold;cursor:pointer;color:white;transition:transform 0.1s}
.btn:active{transform:scale(0.97)}
.btn-green{background:linear-gradient(135deg,#059669,#047857)}
.btn-yellow{background:linear-gradient(135deg,#d97706,#b45309)}
.btn-red{background:linear-gradient(135deg,#dc2626,#b91c1c)}
.btn-blue{background:linear-gradient(135deg,#3b82f6,#2563eb)}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.shock-group{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.shock-group input{background:#0f172a;color:#e2e8f0;border:1px solid #334155;padding:10px;border-radius:8px;width:100px}
.shock-group label{min-width:120px;color:#94a3b8}
.info-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
.info-box{background:#0f172a;border-radius:12px;padding:12px;text-align:center}
.info-box .val{font-size:1.8em;font-weight:bold;color:#3b82f6}
.info-box .lbl{font-size:0.75em;color:#64748b}
.round-row{display:flex;justify-content:space-between;padding:8px 12px;background:#0f172a;border-radius:8px;margin-bottom:4px;font-size:0.9em}
.link-box{background:#0f172a;border-radius:12px;padding:16px;text-align:center;margin-bottom:12px;font-size:0.95em;word-break:break-all}
.link-box a{color:#60a5fa;text-decoration:none}
canvas{width:100%;max-height:300px}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head><body>
<div class="container">
<h1>👋 Čau, Tržní Aréna – Lektor</h1>
<div class="room-badge">Seminář: __ROOM_ID__</div>

<div class="card">
<h2>🔗 Odkaz pro studenty</h2>
<div class="link-box" id="student-link"></div>
</div>

<div class="info-grid">
<div class="info-box"><div class="val" id="s-players">0</div><div class="lbl">Hráčů</div></div>
<div class="info-box"><div class="val" id="s-round">0</div><div class="lbl">Kolo</div></div>
<div class="info-box"><div class="val" id="s-trades">0</div><div class="lbl">Obchodů</div></div>
<div class="info-box"><div class="val" id="s-eq">-</div><div class="lbl">Rovnováha P*</div></div>
</div>

<div class="card">
<h2>🎮 Ovládání kol</h2>
<div class="controls">
<button class="btn btn-green" onclick="send('start_round')">▶️ Spustit kolo</button>
<button class="btn btn-yellow" onclick="send('end_round')">⏹️ Ukončit kolo</button>
<button class="btn btn-red" onclick="if(confirm('Reset celé hry?'))send('reset')">🔄 Reset hry</button>
</div>
</div>

<div class="card">
<h2>💥 Tržní šoky</h2>
<div class="shock-group"><label>Daň (Kč/ks):</label><input id="tax-val" type="number" value="0" min="0"><button class="btn btn-blue" onclick="send('set_tax',document.getElementById('tax-val').value)">Nastavit</button></div>
<div class="shock-group"><label>Cenová podlaha:</label><input id="floor-val" type="number" value="0" min="0"><button class="btn btn-blue" onclick="send('set_floor',document.getElementById('floor-val').value)">Nastavit</button></div>
<div class="shock-group"><label>Cenový strop:</label><input id="ceil-val" type="number" value="0" min="0"><button class="btn btn-blue" onclick="send('set_ceiling',document.getElementById('ceil-val').value)">Nastavit</button>
<button class="btn btn-blue" onclick="send('remove_ceiling')">Zrušit strop</button></div>
</div>

<div class="card">
<h2>📈 Konvergence cen</h2>
<canvas id="price-chart"></canvas>
</div>

<div class="card">
<h2>📋 Historie kol</h2>
<div id="round-history"></div>
</div>
</div>

<script>
const ROOM="__ROOM_ID__";
document.getElementById("student-link").innerHTML=`<a href="${location.origin}" target="_blank">${location.origin}</a><br><br>Kód semináře: <strong style="font-size:1.4em;color:#fcd34d">${ROOM}</strong>`;

const ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/admin/${encodeURIComponent(ROOM)}`);
let chart;

ws.onmessage=e=>{
  const d=JSON.parse(e.data);
  if(d.type==="state"){
    document.getElementById("s-players").textContent=d.players;
    document.getElementById("s-round").textContent=d.round+"/"+d.max_rounds;
    document.getElementById("s-trades").textContent=d.trades;
    document.getElementById("s-eq").textContent=d.eq_price+" Kč";
    let h="";
    d.round_history.forEach(r=>{
      h+=`<div class="round-row"><span>Kolo ${r.round}</span><span>Obchodů: ${r.trades}</span><span>Ø cena: ${r.avg_price} Kč</span><span>P*: ${r.eq_price} Kč</span></div>`;
    });
    document.getElementById("round-history").innerHTML=h||"<p style='color:#64748b'>Zatím žádná kola</p>";
    updateChart(d.round_history);
  }
};

function send(action,value){ws.send(JSON.stringify({action,value:value||0}))}

function updateChart(history){
  const ctx=document.getElementById("price-chart");
  if(!history.length)return;
  const labels=history.map(r=>"Kolo "+r.round);
  const avgPrices=history.map(r=>r.avg_price);
  const eqPrices=history.map(r=>r.eq_price);
  if(chart)chart.destroy();
  chart=new Chart(ctx,{type:"line",data:{labels,datasets:[
    {label:"Průměrná cena",data:avgPrices,borderColor:"#3b82f6",backgroundColor:"rgba(59,130,246,0.1)",tension:0.3,fill:true,pointRadius:6},
    {label:"Rovnovážná cena",data:eqPrices,borderColor:"#f59e0b",borderDash:[5,5],pointRadius:4}
  ]},options:{responsive:true,scales:{y:{beginAtZero:false,grid:{color:"#1e293b"},ticks:{color:"#94a3b8"}},x:{grid:{color:"#1e293b"},ticks:{color:"#94a3b8"}}},plugins:{legend:{labels:{color:"#e2e8f0"}}}}});
}
</script></body></html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
