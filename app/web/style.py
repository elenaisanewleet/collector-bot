"""Оформление веб-отчёта.

Один CSS на все страницы, встроенный в документ: страница открывается по
ссылке, часто с телефона и часто один раз, поэтому лишний сетевой запрос за
стилями здесь стоит дороже, чем несколько килобайт разметки.

Палитра взята из мира задачи: синий — тот самый, что на российском номерном
знаке. Семантические цвета (можно взыскать / спорно / безнадёжно) отделены от
акцента, иначе вердикт спорит с оформлением.
"""

from __future__ import annotations

CSS = """
:root{
  --accent:#1F4E9C; --accent-soft:#E4EBF7;
  --ink:#141922; --ink-2:#454F5E; --ink-3:#727E90;
  --paper:#EBEEF2; --surface:#FFFFFF; --surface-2:#F5F7F9;
  --line:#D3D9E2; --line-soft:#E4E8EE;
  --good:#16624A; --good-bg:#E1EFE9;
  --warn:#8A6410; --warn-bg:#F6EDD8;
  --crit:#A0342A; --crit-bg:#F7E3E0;
  --hero:#141922; --hero-ink:#F2F5F9; --hero-dim:#8B97A8; --hero-soft:#CBD3DE;
  --hero-glow:rgba(31,78,156,.55);
  --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --r:6px;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --accent:#6295E8; --accent-soft:#1B2A44;
    --hero:#0A0E14; --hero-ink:#EDF1F6; --hero-dim:#79859A; --hero-soft:#B9C4D2;
    --hero-glow:rgba(98,149,232,.40);
    --ink:#E7EBF1; --ink-2:#AEB8C6; --ink-3:#7B8797;
    --paper:#0D1117; --surface:#161C25; --surface-2:#1C232E;
    --line:#2A333F; --line-soft:#222A35;
    --good:#4FA98A; --good-bg:#15271F;
    --warn:#CB9C33; --warn-bg:#2A2114;
    --crit:#DC7264; --crit-bg:#2C1917;
  }
}
:root[data-theme="dark"]{
  --accent:#6295E8; --accent-soft:#1B2A44;
  --ink:#E7EBF1; --ink-2:#AEB8C6; --ink-3:#7B8797;
  --paper:#0D1117; --surface:#161C25; --surface-2:#1C232E;
  --line:#2A333F; --line-soft:#222A35;
  --good:#4FA98A; --good-bg:#15271F;
  --warn:#CB9C33; --warn-bg:#2A2114;
  --crit:#DC7264; --crit-bg:#2C1917;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-size:14.5px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
.lbl{font-size:10.5px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}

.shell{max-width:1120px;margin:0 auto;padding:26px 20px 64px;
  display:grid;grid-template-columns:212px 1fr;gap:26px;align-items:start}
@media(max-width:860px){.shell{grid-template-columns:1fr;gap:16px}}

/* ---------------- навигация ---------------- */
nav{position:sticky;top:22px;font-size:13px}
@media(max-width:860px){nav{position:static;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r);padding:10px 12px}}
nav .brand{font-weight:700;letter-spacing:-.01em;margin-bottom:12px;font-size:15px}
nav ol{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1px}
@media(max-width:860px){nav ol{flex-direction:row;flex-wrap:wrap;gap:4px 12px}}
nav a{display:block;padding:4px 8px;border-radius:4px;color:var(--ink-2);
  text-decoration:none;border-left:2px solid transparent}
nav a:hover{background:var(--surface-2);color:var(--accent)}
nav a .n{font-family:var(--mono);color:var(--ink-3);font-size:11.5px;margin-right:7px}
nav .meta{margin-top:14px;color:var(--ink-3);font-size:11.5px;line-height:1.5}
@media(max-width:860px){nav .meta{display:none}}

main{min-width:0;display:flex;flex-direction:column;gap:16px}

/* ---------------- герой ----------------
   Один сильный блок вместо ровного поля одинаковых карточек: страницу
   открывают ради одного ответа — что делать с этим должником. */
.hero{background:var(--hero);color:var(--hero-ink);border-radius:var(--r);
  padding:24px 26px 22px;position:relative;overflow:hidden}
.hero::after{content:"";position:absolute;inset:0 0 auto auto;width:180px;height:100%;
  background:linear-gradient(115deg,transparent 45%,var(--hero-glow) 130%);pointer-events:none}
.hero h1{margin:0;font-size:29px;font-weight:700;letter-spacing:-.022em;text-wrap:balance;
  line-height:1.15}
.hero .sub{margin:7px 0 0;color:var(--hero-dim);font-family:var(--mono);font-size:12.5px}
.hero .badge{display:inline-flex;align-items:center;gap:8px;margin-bottom:14px;
  font-size:12px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
  padding:5px 11px;border-radius:100px;background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.20)}
.hero .badge i{width:7px;height:7px;border-radius:50%;background:currentColor;font-style:normal}
.hero .badge.file{color:#7BD7B0} .hero .badge.order{color:#9CC2FF}
.hero .badge.review{color:#F0C765} .hero .badge.drop{color:#FF9A8B}
.hero .why{margin:14px 0 0;max-width:64ch;color:var(--hero-soft);font-size:15px}
.hero .nums{display:flex;flex-wrap:wrap;gap:26px;margin-top:20px;
  padding-top:17px;border-top:1px solid rgba(255,255,255,.14);position:relative;z-index:1}
.hero .nums div{display:flex;flex-direction:column;gap:2px}
.hero .nums .lbl{color:var(--hero-dim)}
.hero .nums b{font-family:var(--mono);font-size:21px;font-weight:600;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.hero .nums small{color:var(--hero-dim);font-size:11px}

.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:17px 19px}
section.card{scroll-margin-top:22px;position:relative}
section.card > h2{margin:0 0 12px;font-size:12px;font-weight:600;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);
  display:flex;align-items:baseline;gap:9px}
section.card > h2::before{content:attr(data-n);font-family:var(--mono);font-size:11px;
  font-weight:600;color:var(--accent);letter-spacing:0}

/* ---------------- шкала оценки ---------------- */
.gauge{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.gauge svg{flex:0 0 auto}
.gauge .val{font-family:var(--mono);font-size:30px;font-weight:600;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums;line-height:1}
.gauge .cat{font-weight:600;margin-top:3px}
.gauge .conf{min-width:170px;flex:1 1 170px}

/* ---------------- вердикт ---------------- */
.verdict{display:flex;flex-wrap:wrap;gap:13px 20px;align-items:flex-start;
  padding:15px 17px;border-radius:var(--r);border:1px solid}
.verdict.file{background:var(--good-bg);border-color:var(--good)}
.verdict.order{background:var(--accent-soft);border-color:var(--accent)}
.verdict.review{background:var(--warn-bg);border-color:var(--warn)}
.verdict.drop{background:var(--crit-bg);border-color:var(--crit)}
.verdict .title{font-size:17px;font-weight:700;letter-spacing:-.01em;margin-top:1px}
.verdict.file .title{color:var(--good)} .verdict.order .title{color:var(--accent)}
.verdict.review .title{color:var(--warn)} .verdict.drop .title{color:var(--crit)}
.verdict .why{flex:1 1 280px;min-width:0;color:var(--ink-2)}
.econ{display:flex;gap:20px;flex-wrap:wrap;margin-left:auto}
.econ div{display:flex;flex-direction:column;gap:1px}
.econ b{font-family:var(--mono);font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.econ small{color:var(--ink-3);font-size:11px}

/* ---------------- факты ---------------- */
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  border:1px solid var(--line-soft);border-radius:var(--r);overflow:hidden}
.fact{padding:11px 13px;border-right:1px solid var(--line-soft);border-bottom:1px solid var(--line-soft)}
.fact .v{font-family:var(--mono);font-size:13px;margin-top:2px;font-variant-numeric:tabular-nums}
.fact .v.hit{color:var(--crit);font-weight:600}
.fact .v.ok{color:var(--good)}
.fact .v.none{color:var(--ink-3)}
.fact .src{font-size:10.5px;color:var(--ink-3);margin-top:3px}

/* ---------------- таблицы ---------------- */
.scroll{overflow-x:auto;border:1px solid var(--line-soft);border-radius:var(--r)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line-soft);vertical-align:top}
th{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);
  font-weight:600;background:var(--surface-2);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
td.r{text-align:right}
tbody tr:hover{background:var(--surface-2)}

.copy{cursor:pointer;border-bottom:1px dotted var(--ink-3)}
.copy:hover{color:var(--accent);border-bottom-color:var(--accent)}

/* ---------------- бейджи и пустые состояния ---------------- */
.tag{display:inline-block;font-size:10.5px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;padding:2px 7px;border-radius:3px;white-space:nowrap}
.tag.good{background:var(--good-bg);color:var(--good)}
.tag.warn{background:var(--warn-bg);color:var(--warn)}
.tag.crit{background:var(--crit-bg);color:var(--crit)}
.tag.mute{background:var(--surface-2);color:var(--ink-3)}
.tag.accent{background:var(--accent-soft);color:var(--accent)}

.empty{color:var(--ink-2)}
.empty.unchecked{color:var(--warn)}
.note{color:var(--ink-3);font-size:12.5px;margin:9px 0 0}

/* ---------------- источники ---------------- */
.sources{display:flex;flex-direction:column;gap:1px}
.srow{display:flex;gap:12px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line-soft)}
.srow:last-child{border-bottom:0}
.srow .nm{flex:0 0 168px;font-weight:500}
.srow .st{color:var(--ink-2);font-size:13px}

/* ---------------- факторы оценки ---------------- */
.factors{display:flex;flex-direction:column;gap:1px}
.frow{display:flex;gap:12px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--line-soft)}
.frow:last-child{border-bottom:0}
.frow .d{flex:0 0 46px;text-align:right;font-family:var(--mono);font-weight:600;
  font-variant-numeric:tabular-nums}
.frow.plus .d{color:var(--good)} .frow.minus .d{color:var(--crit)}

.meter{height:6px;border-radius:3px;background:var(--line-soft);overflow:hidden;margin:9px 0 4px}
.meter i{display:block;height:100%;border-radius:3px;background:var(--accent)}

footer{color:var(--ink-3);font-size:12px;max-width:78ch;padding:2px 2px 0}

/* ---------------- очередь ---------------- */
.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.cell{padding:12px 15px;border-right:1px solid var(--line-soft);display:flex;flex-direction:column;gap:2px}
.cell:last-child{border-right:0}
.cell b{font-family:var(--mono);font-size:20px;font-weight:600;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.cell.good b{color:var(--good)} .cell.warn b{color:var(--warn)} .cell.crit b{color:var(--crit)}
.cell.accent b{color:var(--accent)}
.cell small{color:var(--ink-3);font-size:11.5px}

.filters{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 13px}
.filters button{font:inherit;font-size:12.5px;font-weight:500;padding:5px 11px;cursor:pointer;
  background:var(--surface);color:var(--ink-2);border:1px solid var(--line);border-radius:var(--r)}
.filters button[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
.stripe{display:inline-block;width:3px;height:13px;border-radius:2px;vertical-align:-2px;margin-right:8px}
.stripe.file{background:var(--good)} .stripe.order{background:var(--accent)}
.stripe.review{background:var(--warn)} .stripe.drop{background:var(--crit)}

@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
@media print{nav,.filters{display:none}.shell{grid-template-columns:1fr}body{background:#fff}}
"""
