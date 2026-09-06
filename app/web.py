"""Phone-friendly control panel.

The engine never starts itself: something has to press Start, and that
something is a person looking at this page. The panel is therefore always
password protected - it can start and stop a martingale, which is not a control
to leave open on a public URL.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from utils.logging import get_logger

log = get_logger("web")

app = FastAPI(title="XAU Stop-And-Reverse Martingale")

#: Wired up by main.py at boot.
engine = None
cfg = None
_sessions: set[str] = set()

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>XAU Flipper</title><style>
:root{color-scheme:dark}
body{margin:0;background:#0b0d12;color:#e6e9ef;font:15px/1.5 system-ui,-apple-system,sans-serif}
.wrap{max-width:560px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:14px}
.card{background:#141822;border:1px solid #222839;border-radius:12px;padding:14px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1c2130}
.row:last-child{border-bottom:0}
.k{color:#8b93a7}.v{font-variant-numeric:tabular-nums;font-weight:600}
.pos{color:#3ddc97}.neg{color:#ff6b6b}.warn{color:#ffb454}
button{width:100%;padding:14px;border:0;border-radius:10px;font-size:16px;font-weight:600;
margin-bottom:8px;color:#fff}
.start{background:#1f7a4d}.stop{background:#7a5c1f}.panic{background:#8c2b2b}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600}
.on{background:#1f7a4d}.off{background:#3a4150}.halt{background:#8c2b2b}
input{width:100%;padding:12px;border-radius:10px;border:1px solid #2a3145;background:#0f131b;color:#e6e9ef;font-size:16px;margin-bottom:8px}
</style></head><body><div class="wrap">
<h1>XAU Stop-And-Reverse</h1>
<div class="sub" id="sub">loading…</div>
<div class="card" id="status">…</div>
<form method="post" action="/start"><button class="start">Start</button></form>
<form method="post" action="/stop"><button class="stop">Stop</button></form>
<form method="post" action="/panic" onsubmit="return confirm('Close every position now?')">
<button class="panic">Close everything now</button></form>
</div><script>
function row(k,v,c){return '<div class="row"><span class="k">'+k+'</span><span class="v '+(c||'')+'">'+v+'</span></div>'}
async function tick(){
 try{const r=await fetch('/api/status');const s=await r.json();
  const st=s.halted?'<span class="badge halt">HALTED</span>':(s.running?'<span class="badge on">RUNNING</span>':'<span class="badge off">STOPPED</span>');
  document.getElementById('sub').innerHTML=st+' &middot; '+s.symbol+' &middot; '+(s.live?'LIVE':'PAPER');
  let h='';
  h+=row('Bid / Ask',s.bid+' / '+s.ask);
  h+=row('Spread',s.spread_pips+' pips',s.spread_pips>s.max_spread_pips?'warn':'');
  h+=row('Balance',s.balance.toFixed(2));
  h+=row('Day P&L',s.day_pnl.toFixed(2),s.day_pnl<0?'neg':'pos');
  h+=row('Basket P&L',s.basket_pnl.toFixed(2)+' / '+s.target,s.basket_pnl<0?'neg':'pos');
  h+=row('Positions',s.positions+' ('+s.open_lots+' / '+s.max_lots+' lots)');
  h+=row('Pending',s.pendings);
  h+=row('Step',s.step+' / '+s.max_steps);
  h+=row('Next lot',s.next_lots);
  h+=row('Cycles closed',s.cycles_closed);
  if(s.blackout) h+=row('News blackout','active','warn');
  if(s.halted) h+=row('Halt reason',s.halt_reason,'neg');
  if(s.error) h+=row('Error',s.error,'neg');
  h+=row('Last action',s.last_action);
  document.getElementById('status').innerHTML=h;
 }catch(e){document.getElementById('sub').textContent='connection lost';}
}
tick();setInterval(tick,2000);
</script></body></html>"""

LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>XAU Flipper</title>
<style>:root{color-scheme:dark}body{margin:0;background:#0b0d12;color:#e6e9ef;
font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;height:100vh}
form{width:min(360px,88vw)}input{width:100%;padding:14px;border-radius:10px;
border:1px solid #2a3145;background:#0f131b;color:#e6e9ef;font-size:16px;margin-bottom:10px}
button{width:100%;padding:14px;border:0;border-radius:10px;background:#1f7a4d;
color:#fff;font-size:16px;font-weight:600}p{color:#ff6b6b;font-size:14px}</style>
</head><body><form method="post" action="/login">
<input type="password" name="password" placeholder="Panel password" autofocus>
<button>Enter</button>__ERR__</form></body></html>"""


def _authed(request: Request) -> bool:
    token = request.cookies.get("sid", "")
    return bool(token) and token in _sessions


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _authed(request):
        return HTMLResponse(LOGIN.replace("__ERR__", ""))
    return HTMLResponse(PAGE)


@app.post("/login")
async def login(password: str = Form("")):
    if not cfg or not secrets.compare_digest(password, cfg.panel_password):
        return HTMLResponse(LOGIN.replace("__ERR__", "<p>Wrong password</p>"),
                            status_code=401)
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("sid", token, httponly=True, samesite="lax", max_age=86400)
    return response


@app.get("/api/status")
async def status(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if engine is None:
        return JSONResponse({"error": "booting"}, status_code=503)
    return JSONResponse(engine.status())


@app.post("/start")
async def start(request: Request):
    if not _authed(request):
        return RedirectResponse("/", status_code=303)
    engine.start()
    return RedirectResponse("/", status_code=303)


@app.post("/stop")
async def stop(request: Request):
    if not _authed(request):
        return RedirectResponse("/", status_code=303)
    engine.stop()
    return RedirectResponse("/", status_code=303)


@app.post("/panic")
async def panic(request: Request):
    if not _authed(request):
        return RedirectResponse("/", status_code=303)
    await engine.panic_close()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
async def health():
    """Unauthenticated: Railway needs it, and it leaks nothing."""
    if engine is None:
        return JSONResponse({"status": "booting"}, status_code=503)
    healthy = engine.api.connected
    return JSONResponse({"status": "ok" if healthy else "degraded"},
                        status_code=200 if healthy else 503)
