"""Tableau de bord local, en LECTURE SEULE : `python -m bot dashboard`, puis http://127.0.0.1:8050.

Problème résolu : voir en direct, sur le PC (ou via un tunnel SSH depuis le VPS), ce que fait
le robot — prix, santé du flux Kraken, préchauffage, risque, blocages, positions, journal —
sans rien pouvoir changer. Le contrôle reste /status et /stop (Telegram) ou la ligne de commande.

Processus SÉPARÉ du robot : la base SQLite est ouverte en lecture seule (mode=ro) à chaque
requête. Un plantage ou un arrêt du tableau de bord n'a aucun effet sur le robot, et le robot
ne dépend pas de lui (il écrit seulement un instantané d'état chaque seconde, voir
Engine.write_snapshot). Bibliothèque standard uniquement ; page sans ressource externe.
"""
from __future__ import annotations

import json
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STALE_SNAPSHOT_S = 10        # instantané plus vieux : robot arrêté, figé ou pas encore démarré
MAX_POINTS = 1500            # points du graphique de prix (sous-échantillonnage au-delà)


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def read_dashboard(db_path: str | Path, now_ms: int | None = None, hours: float = 3.0) -> dict:
    """Tout ce qu'affiche la page, lu dans la base d'état du robot (aucune écriture)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    out: dict = {"now_ms": now_ms, "db": str(db_path), "available": False}
    try:
        con = _connect_ro(Path(db_path))
    except sqlite3.Error as e:
        out["error"] = f"base illisible : {e}"
        return out
    if con is None:
        out["error"] = "base d'état introuvable : le robot n'a jamais été lancé avec cette configuration"
        return out
    try:
        meta = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM meta")}
        snap = meta.get("live_snapshot")
        out.update(available=True, snapshot=snap,
                   snapshot_age_s=None if not snap else (now_ms - int(snap["ts_ms"])) / 1000,
                   stopped=bool(meta.get("stopped", False)), last_start_ms=meta.get("last_start_ms"))
        out["stale"] = out["snapshot_age_s"] is None or out["snapshot_age_s"] > STALE_SNAPSHOT_S
        # fenêtres relatives au dernier état connu : robot arrêté => on voit ses dernières heures
        ref = min(now_ms, int(snap["ts_ms"])) if snap and snap.get("ts_ms") else now_ms
        since = ref - int(hours * 3_600_000)
        # graphique : une clôture de barre 5 s par décision consignée (aucune requête au marché)
        rows = con.execute("SELECT bar_ts, close FROM decisions WHERE bar_ts>=? AND close IS NOT NULL "
                           "ORDER BY bar_ts", (since,)).fetchall()
        step = max(1, len(rows) // MAX_POINTS)
        out["prices"] = [[r["bar_ts"], r["close"]] for r in rows[::step]]
        if rows and step > 1 and out["prices"][-1][0] != rows[-1]["bar_ts"]:
            out["prices"].append([rows[-1]["bar_ts"], rows[-1]["close"]])
        day = ref - 86_400_000
        out["decisions_24h"] = {r[0]: r[1] for r in con.execute(
            "SELECT outcome, COUNT(*) FROM decisions WHERE bar_ts>=? GROUP BY outcome", (day,))}
        out["open_positions"] = [dict(r) for r in con.execute(
            "SELECT * FROM positions WHERE status IN ('pending_entry','open') ORDER BY signal_ts")]
        out["closed_positions"] = [dict(r) for r in con.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY exit_ms DESC LIMIT 30")]
        tot = con.execute("SELECT dry, COUNT(*), COALESCE(SUM(net),0), COALESCE(SUM(net>0),0) "
                          "FROM positions WHERE status='closed' GROUP BY dry").fetchall()
        out["totals"] = {("dry" if r[0] else "model"): {"n": r[1], "net": r[2], "wins": r[3]} for r in tot}
        out["events"] = [dict(r) for r in con.execute(
            "SELECT ts_ms, level, kind, msg FROM events ORDER BY id DESC LIMIT 40")]
    except sqlite3.Error as e:
        out.update(available=False, error=f"lecture impossible : {e}")
    finally:
        con.close()
    return out


class _Handler(BaseHTTPRequestHandler):
    db_path: Path = Path()

    def do_GET(self):  # noqa: N802 (API http.server)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif path == "/api/state":
            body = json.dumps(read_dashboard(self.db_path), default=str).encode("utf-8")
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"introuvable")

    def do_POST(self):  # noqa: N802 — lecture seule : aucune action possible depuis la page
        self._send(405, "text/plain; charset=utf-8", "lecture seule".encode("utf-8"))

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):   # pas de bruit dans la console à chaque rafraîchissement
        pass


def make_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 8050) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"db_path": Path(db_path)})
    return ThreadingHTTPServer((host, port), handler)


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8050, open_browser: bool = True) -> int:
    srv = make_server(db_path, host, port)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{srv.server_address[1]}/"
    print(f"Tableau de bord (lecture seule) : {url}  — base {db_path}")
    print("Ctrl+C pour l'arrêter (le robot continue de tourner).")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"ATTENTION : écoute sur {host} — la page est visible depuis le réseau. "
              "Sur le VPS, préférer 127.0.0.1 + tunnel SSH (voir RUNBOOK).")
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — pas de navigateur (serveur) : sans importance
            pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robot BTC — direct</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#5d6670;--line:#dfe3e8;--accent:#2f6fde;
--ok:#1f8a4c;--warn:#b26a00;--bad:#c62828;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--fg:#e6e9ed;--muted:#9aa4af;
--line:#2c333b;--accent:#6ea2ff;--ok:#4cc27e;--warn:#e0a03a;--bad:#ff6b6b;--chip:#252c34}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;padding:14px 16px;border-bottom:1px solid var(--line)}
h1{font-size:17px;margin:0}
.sub{color:var(--muted);font-size:13px}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;padding:12px 16px;max-width:1400px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;min-width:0}
.wide{grid-column:1/-1}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px}
.big{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-variant-numeric:tabular-nums}
.kv span:nth-child(odd){color:var(--muted)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.chip{display:inline-block;background:var(--chip);border-radius:4px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12.5px}
.chip.bad{color:var(--bad)}.chip.warn{color:var(--warn)}
.banner{display:none;margin:12px 16px 0;padding:10px 14px;border-radius:8px;border:1px solid var(--bad);color:var(--bad);background:var(--card)}
.bar{height:8px;background:var(--chip);border-radius:4px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}
.scroll{overflow-x:auto;max-height:340px;overflow-y:auto}
td.msg{white-space:normal;min-width:240px}
svg{width:100%;height:260px;display:block}
.axis{fill:var(--muted);font-size:11px}
.grid{stroke:var(--line);stroke-width:1}
.price{fill:none;stroke:var(--accent);stroke-width:1.6}
.note{color:var(--muted);font-size:12px;margin-top:6px}
</style>
</head>
<body>
<header>
  <h1>Robot BTC — Kraken PF_XBTUSD</h1>
  <span class="sub" id="hdr">chargement…</span>
  <span class="sub">Lecture seule · rafraîchi toutes les 2 s</span>
</header>
<div class="banner" id="banner"></div>
<main>
  <section class="card"><h2>Prix</h2><div class="big" id="px">—</div><div class="kv" id="mkt"></div></section>
  <section class="card"><h2>Flux Kraken</h2><div class="kv" id="feed"></div></section>
  <section class="card"><h2>Préchauffage</h2><div id="warm"></div></section>
  <section class="card"><h2>Risque</h2><div class="kv" id="risk"></div></section>
  <section class="card"><h2>Entrées autorisées ?</h2><div id="gates"></div><div id="alerts"></div></section>
  <section class="card"><h2>Position</h2><div id="pos"></div></section>
  <section class="card wide"><h2>Prix (clôtures 5 s, 3 dernières heures)</h2><svg id="chart" role="img" aria-label="Graphique du prix"></svg>
    <div class="note">&#9650; entrée long · &#9660; entrée short · &#9679; sortie. Trades « dry » = règle de référence, sans valeur probante.</div></section>
  <section class="card wide"><h2>Décisions (24 h) et résultats paper</h2><div id="dec"></div></section>
  <section class="card wide"><h2>Trades clos</h2><div class="scroll"><table id="trades"></table></div></section>
  <section class="card wide"><h2>Journal</h2><div class="scroll"><table id="events"></table></div></section>
</main>
<script>
const GATES={stopped:["/stop actif","bad"],reconcile_divergence:["divergence d'état","bad"],
feed_disconnected:["flux déconnecté","bad"],stale_feed:["flux périmé","bad"],warmup:["préchauffage 25 h","warn"],
clock_unchecked:["horloge non contrôlée","warn"],clock_offset:["horloge décalée","bad"],feed_lag:["flux en retard","bad"],
market_suspended:["marché suspendu","bad"],position_open:["position déjà ouverte",""],stale_gap:["pause après trou de données","warn"],
instrument_rules:["règles instrument différentes","bad"],trade_seq_gap:["pause après trou de séquence","warn"],
reconnect_gap:["pause après reconnexion","warn"],data_anomaly:["pause après donnée suspecte","warn"],late_bar:["barre en retard","warn"]};
const OUT={no_signal:"pas de signal",paused:"en pause",blocked:"signal bloqué",veto:"veto risque",entry:"entrée"};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f=(x,d=2)=>x==null||isNaN(x)?"—":Number(x).toLocaleString("fr-FR",{minimumFractionDigits:d,maximumFractionDigits:d});
const t=ms=>ms?new Date(ms).toISOString().replace("T"," ").slice(0,19)+" UTC":"—";
const dur=s=>{if(s==null)return"—";s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} h ${String(m).padStart(2,"0")} min`:m?`${m} min ${s%60} s`:`${s} s`};
const kv=rows=>rows.map(([k,v,c])=>`<span>${esc(k)}</span><span class="${c||""}">${v}</span>`).join("");
const side=s=>s>0?"LONG":"SHORT";
function chart(prices,pos){
  const svg=document.getElementById("chart"),W=svg.clientWidth||800,H=260,L=64,R=10,T=10,B=24;
  if(!prices.length){svg.innerHTML=`<text class="axis" x="${L}" y="${H/2}">Aucune barre enregistrée pour l'instant.</text>`;return}
  const xs=prices.map(p=>p[0]),ys=prices.map(p=>p[1]);
  const x0=xs[0],x1=Math.max(xs[xs.length-1],x0+1);let y0=Math.min(...ys),y1=Math.max(...ys);
  const pad=Math.max((y1-y0)*0.08,1);y0-=pad;y1+=pad;
  const X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=y=>T+(y1-y)/(y1-y0)*(H-T-B);
  let g="";for(let i=0;i<=4;i++){const v=y0+(y1-y0)*i/4;g+=`<line class="grid" x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="axis" x="${L-6}" y="${Y(v)+4}" text-anchor="end">${f(v,0)}</text>`}
  for(let i=0;i<=4;i++){const v=x0+(x1-x0)*i/4;g+=`<text class="axis" x="${X(v)}" y="${H-6}" text-anchor="${i===4?"end":"middle"}">${new Date(v).toISOString().slice(11,16)}</text>`}
  const d=prices.map((p,i)=>(i?"L":"M")+X(p[0]).toFixed(1)+","+Y(p[1]).toFixed(1)).join("");
  let m="";for(const p of pos){
    if(p.entry_ms>=x0&&p.entry_px){const x=X(p.entry_ms),y=Y(p.entry_px),c=p.side>0?"var(--ok)":"var(--bad)";
      m+=p.side>0?`<path d="M${x},${y-7}l6,10h-12z" fill="${c}"/>`:`<path d="M${x},${y+7}l6,-10h-12z" fill="${c}"/>`}
    if(p.exit_ms>=x0&&p.exit_px)m+=`<circle cx="${X(p.exit_ms)}" cy="${Y(p.exit_px)}" r="4" fill="none" stroke="var(--fg)" stroke-width="1.5"/>`}
  svg.innerHTML=g+`<path class="price" d="${d}"/>`+m;
}
async function refresh(){
  let s;try{s=await (await fetch("api/state",{cache:"no-store"})).json()}catch(e){
    const b=document.getElementById("banner");b.style.display="block";b.textContent="Tableau de bord injoignable (processus arrêté ?)";return}
  const b=document.getElementById("banner"),sn=s.snapshot||{};
  if(!s.available){b.style.display="block";b.textContent=s.error||"base indisponible";return}
  if(s.stale){b.style.display="block";b.textContent=s.snapshot_age_s==null?"Aucun instantané : le robot n'a pas encore tourné avec cette version.":
    `Robot arrêté ou figé : dernier état il y a ${dur(s.snapshot_age_s)} (${t(sn.ts_ms)}). Les valeurs ci-dessous sont celles de ce moment-là.`}
  else b.style.display="none";
  document.getElementById("hdr").textContent=`mode ${sn.mode||"paper"} · stratégie ${sn.strategy||"?"} · config ${sn.research_hash||"?"} · démarré ${t(sn.started_ms||s.last_start_ms)}`;
  const mk=sn.market||{},fd=sn.feed||{},rk=sn.risk||{},sp=mk.spread||{};
  document.getElementById("px").textContent=mk.last_px?f(mk.last_px,0)+" $":"—";
  document.getElementById("mkt").innerHTML=kv([["Bid / ask",`${f(mk.bid,0)} / ${f(mk.ask,0)}`],["Mark",f(mk.mark_px,1)],
    ["Spread médian",sp.median_ticks!=null?`${f(sp.median_ticks,1)} tick (p90 ${f(sp.p90_ticks,1)})`:"—"],
    ["Funding relatif (1 h)",mk.relative_funding_rate!=null?(mk.relative_funding_rate*100).toFixed(5)+" %":"—"],["Dernier trade",t(mk.last_trade_ts)]]);
  const age=fd.last_msg_age_s;
  document.getElementById("feed").innerHTML=kv([["État",fd.connected?"connecté":"DÉCONNECTÉ",fd.connected?"ok":"bad"],
    ["Dernier message",age==null?"—":`il y a ${f(age,1)} s`,age==null||age>15?"bad":"ok"],["Retard médian",fd.median_lag_ms==null?"—":`${fd.median_lag_ms} ms`,fd.median_lag_ms>2000?"bad":""],
    ["Horloge (local - Kraken)",fd.clock_offset_ms==null?"non contrôlée":`${fd.clock_offset_ms} ms`,fd.clock_offset_ms==null||Math.abs(fd.clock_offset_ms)>1000?"warn":"ok"],
    ["Connexions",fd.connections??"—"],["Enregistrement",sn.recorder?`${(sn.recorder.lines||0).toLocaleString("fr-FR")} lignes, ${f(sn.recorder.mb,1)} Mo`:"—"]]);
  const tot=sn.warmup_total_s||1,left=sn.warmup_left_s;
  document.getElementById("warm").innerHTML=left==null?"—":left>0?`<div class="big">${dur(left)}</div><div class="sub">restant avant la première entrée possible</div><div class="bar"><i style="width:${(100*(tot-left)/tot).toFixed(1)}%"></i></div>`:
    `<div class="big ok">terminé</div><div class="sub">les facteurs disposent de 24 h d'historique</div>`;
  const dd=(rk.peak_equity||0)-(rk.equity||0),ddmax=rk.max_drawdown_usdt;
  document.getElementById("risk").innerHTML=kv([["Capital paper",f(rk.equity)+" $"],["Sommet",f(rk.peak_equity)+" $"],
    ["Repli depuis le sommet",`${f(dd)} $ / ${f(ddmax,0)} $`,ddmax&&dd>=ddmax*2/3?"bad":ddmax&&dd>=ddmax/3?"warn":""],
    ["Trades du jour",`${rk.day_trades??"—"} / ${rk.max_trades_per_day??"—"}`],
    ["Taille",rk.size_mult!=null?`× ${f(rk.size_mult,2)}`:"—",rk.size_mult<1?"warn":""],
    ["Coupe-circuit",rk.killed?"ACTIF — "+esc(rk.kill_reason):"non",rk.killed?"bad":"ok"],
    ["Arrêt jusqu'à",rk.halt_until_ms?t(rk.halt_until_ms):"—",rk.halt_until_ms?"warn":""],["/stop",s.stopped?"ACTIF":"non",s.stopped?"bad":"ok"]]);
  const gates=sn.gates||[];
  document.getElementById("gates").innerHTML=gates.length?gates.map(g=>{const[l,c]=GATES[g]||[g,"warn"];return`<span class="chip ${c}">${esc(l)}</span>`}).join(""):
    `<div class="big ok">oui</div><div class="sub">${sn.strategy==="none"?"mais aucune stratégie validée : le robot observe et enregistre seulement":"le modèle décide à chaque barre"}</div>`;
  const al=Object.values(sn.alerts||{});
  document.getElementById("alerts").innerHTML=al.length?`<div class="note">Alertes actives :</div>`+al.map(a=>`<div class="bad">${esc(a)}</div>`).join(""):"";
  const op=s.open_positions||[];
  document.getElementById("pos").innerHTML=op.length?op.map(p=>{
    const pnl=p.entry_px&&mk.last_px?(mk.last_px-p.entry_px)*p.side*p.qty:null;
    return `<div class="big ${p.side>0?"ok":"bad"}">${side(p.side)} ${p.qty} BTC</div><div class="kv">${kv([["Statut",esc(p.status)],["Entrée",f(p.entry_px,0)],
      ["Stop / TP",`${f(p.stop_px,0)} / ${f(p.tp_px,0)}`],["Sortie max",t(p.deadline_ms)],["PnL latent (brut)",pnl==null?"—":f(pnl)+" $",pnl>0?"ok":pnl<0?"bad":""],
      ["Type",p.dry?"dry (référence)":"modèle"]])}</div>`}).join(""):`<div class="sub">Aucune position.</div>`;
  chart(s.prices||[],[...op,...(s.closed_positions||[])]);
  const dc=s.decisions_24h||{},tt=s.totals||{};
  const res=k=>tt[k]?`${tt[k].n} trades, net ${f(tt[k].net)} $, ${tt[k].n?Math.round(100*tt[k].wins/tt[k].n):0} % gagnants`:"aucun";
  document.getElementById("dec").innerHTML=Object.keys(dc).length?Object.entries(dc).map(([k,v])=>`<span class="chip">${esc(OUT[k]||k)} : ${v.toLocaleString("fr-FR")}</span>`).join(""):"<span class='sub'>aucune décision</span>";
  document.getElementById("dec").innerHTML+=`<div class="kv" style="margin-top:8px">${kv([["Modèle",res("model")],["Dry (référence)",res("dry")]])}</div>`;
  const cp=s.closed_positions||[];
  document.getElementById("trades").innerHTML=cp.length?"<tr><th>Sortie</th><th>Sens</th><th>Qté</th><th>Entrée</th><th>Sortie</th><th>Motif</th><th>Net $</th><th>Type</th></tr>"+
    cp.map(p=>`<tr><td>${t(p.exit_ms)}</td><td>${side(p.side)}</td><td>${p.qty}</td><td>${f(p.entry_px,0)}</td><td>${f(p.exit_px,0)}</td><td>${esc(p.exit_reason)}</td><td class="${p.net>0?"ok":"bad"}">${f(p.net)}</td><td>${p.dry?"dry":"modèle"}</td></tr>`).join(""):"<tr><td class='sub'>Aucun trade clos.</td></tr>";
  const ev=s.events||[];
  document.getElementById("events").innerHTML=ev.map(e=>`<tr><td>${t(e.ts_ms)}</td><td class="${e.level==="CRIT"?"bad":e.level==="WARN"?"warn":""}">${esc(e.level)}</td><td>${esc(e.kind)}</td><td class="msg">${esc(e.msg)}</td></tr>`).join("");
}
refresh();setInterval(refresh,2000);addEventListener("resize",()=>refresh());
</script>
</body>
</html>
"""
