"""Оформление веб-отчёта.

Один CSS на все страницы, встроенный в документ: страница открывается по
ссылке, часто с телефона и часто один раз, поэтому лишний сетевой запрос за
стилями здесь стоит дороже, чем несколько килобайт разметки. По той же причине
шрифты системные — за страницей персональные данные, и ходить за ними к
третьей стороне нечего.

Две вещи определяют всё остальное.

Первое: состояние источника нигде не держится на цвете. У каждого состояния
есть знак (``✓ — ○ ? ! ✗``), форма чипа (сплошной / контурный / штрихованный) и
слово. Страницу распечатывают на чёрно-белом принтере и подшивают к делу, и
«не проверено», ставшее там неотличимым от «чисто», — это то же нарушение
инварианта, только на бумаге.

Второе: печать — не побочный режим, а вторая форма того же документа. A4,
поля, колонтитул, таблицы не рвутся, свёрнутое раскрывается, интерфейсное
уходит.

Палитра взята из мира задачи: синий — тот самый, что на российском номерном
знаке, и идентификаторы (номер производства, дело, ИНН, госномер) набраны
моноширинным в рамке-«плашке» — так их удобно и копировать, и сверять.
"""

from __future__ import annotations

CSS = """
:root{
  --accent:#1F4E9C; --accent-soft:#E4EBF7; --accent-ink:#1A4488;
  --ink:#141922; --ink-2:#414B5A; --ink-3:#5C6878;
  --paper:#EBEEF2; --surface:#FFFFFF; --surface-2:#F4F6F9;
  --line:#CDD4DE; --line-soft:#E1E6ED;
  --good:#155C46; --good-bg:#E1EFE9;
  --warn:#7A5809; --warn-bg:#F6EDD8;
  /* Штриховка «не проверено». Отдельным токеном, потому что она рисуется
     поверх поверхности, а не поверх --warn-bg: тёмно-янтарный на 13% читается
     на белом и полностью исчезает на #161C25. Это уже стояло на проде —
     единственный признак «источник молчал» в тёмной теме был невидим. */
  --hatch:rgba(122,88,9,.13);
  --crit:#95302A; --crit-bg:#F7E3E0;
  --hero:#141922; --hero-ink:#F4F6FA; --hero-dim:#9AA6B6; --hero-soft:#CFD7E2;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","Segoe UI Mono","Roboto Mono",Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  /* Одна прогрессия на всю страницу вместо подкрученных дробных пикселей. */
  --t-3xs:.6875rem; --t-2xs:.75rem; --t-xs:.8125rem; --t-sm:.875rem;
  --t-md:1rem; --t-lg:1.125rem; --t-xl:1.375rem; --t-2xl:1.75rem; --t-3xl:2.125rem;
  --s-1:4px; --s-2:8px; --s-3:12px; --s-4:16px; --s-5:20px; --s-6:24px; --s-8:32px;
  --r:6px;
}
@media (prefers-color-scheme:dark){
  :root{
    --accent:#7CA7EC; --accent-soft:#1B2A44; --accent-ink:#9CBEF5;
    --ink:#E7EBF1; --ink-2:#B6C0CE; --ink-3:#96A2B2;
    --paper:#0D1117; --surface:#161C25; --surface-2:#1D2531;
    --line:#37414F; --line-soft:#2C3644;
    --good:#5CBA98; --good-bg:#15271F;
    --warn:#D6A73C; --warn-bg:#2A2114;
    --hatch:rgba(214,167,60,.20);
    --crit:#E38375; --crit-bg:#2C1917;
    /* Герой в тёмной теме светлее фона: иначе единственный блок, ради которого
       открывают страницу, растворяется в подложке. */
    --hero:#1E2733; --hero-ink:#F1F4F9; --hero-dim:#9AA6B6; --hero-soft:#C9D2DE;
  }
}
*{box-sizing:border-box}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-size:var(--t-sm);line-height:1.55;-webkit-font-smoothing:antialiased}
@media(max-width:860px){body{font-size:var(--t-md)}}
a{color:var(--accent)}
h1,h2,h3{text-wrap:balance}
.card h3{margin:var(--s-5) 0 var(--s-2);font-size:var(--t-sm)}
.lbl{font-size:var(--t-3xs);font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-3)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip-path:inset(50%);white-space:nowrap;border:0}

.shell{max-width:1120px;margin:0 auto;padding:var(--s-6) var(--s-5) 64px;
  display:grid;grid-template-columns:212px 1fr;gap:var(--s-6);align-items:start}
@media(max-width:860px){.shell{grid-template-columns:1fr;gap:var(--s-4)}
  /* На телефоне первым идёт ответ, а не оглавление к нему. */
  main{order:1} nav{order:2}}

/* ---------------- навигация ---------------- */
nav{position:sticky;top:22px;font-size:var(--t-xs)}
@media(max-width:860px){nav{position:static;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r);padding:var(--s-2) var(--s-3)}}
nav .brand{font-weight:700;letter-spacing:-.01em;margin-bottom:var(--s-3);font-size:var(--t-md)}
nav ol{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
@media(max-width:860px){nav ol{flex-direction:row;flex-wrap:wrap;gap:var(--s-2)}}
nav a{display:block;padding:var(--s-1) var(--s-2);border-radius:4px;color:var(--ink-2);
  text-decoration:none}
nav a:hover{background:var(--surface-2);color:var(--accent)}
@media(max-width:860px){nav a{padding:10px 12px;background:var(--surface-2)}}
nav .meta{margin-top:var(--s-4);color:var(--ink-3);font-size:var(--t-2xs);line-height:1.5}
@media(max-width:860px){nav .meta{margin-top:var(--s-3)}}

main{min-width:0;display:flex;flex-direction:column;gap:var(--s-4)}

/* ---------------- баннер демо-режима ----------------
   Стоит выше героя и намеренно шумит: страница с выдуманными данными не
   должна выглядеть как проверка. */
.demo{background:var(--warn-bg);color:var(--warn);border:2px solid var(--warn);
  border-radius:var(--r);padding:var(--s-3) var(--s-4);font-weight:600;
  font-size:var(--t-sm);
  background-image:repeating-linear-gradient(135deg,transparent 0 10px,
    var(--hatch) 10px 20px)}

/* ---------------- герой ----------------
   Один сильный блок вместо ровного поля одинаковых карточек: страницу
   открывают ради одного ответа — что делать с этим должником. */
.hero{background:var(--hero);color:var(--hero-ink);border-radius:var(--r);
  border:1px solid var(--line);padding:var(--s-6) var(--s-6) var(--s-5)}
.hero h1{margin:0;font-size:var(--t-2xl);font-weight:700;letter-spacing:-.02em;line-height:1.15}
.hero .sub{margin:var(--s-2) 0 0;color:var(--hero-dim);font-family:var(--mono);
  font-size:var(--t-2xs)}
.hero .badge{display:inline-flex;align-items:center;gap:var(--s-2);margin-bottom:var(--s-3);
  font-size:var(--t-2xs);font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  padding:6px 12px;border-radius:100px;background:rgba(255,255,255,.10);
  border:1px solid currentColor}
.hero .badge i{width:7px;height:7px;border-radius:50%;background:currentColor;font-style:normal}
.hero .badge.file{color:#7BD7B0} .hero .badge.order{color:#9CC2FF}
.hero .badge.review{color:#F0C765} .hero .badge.drop{color:#FF9A8B}
.hero .why{margin:var(--s-3) 0 0;max-width:64ch;color:var(--hero-soft);font-size:var(--t-md)}
.hero ul.reasons{margin:var(--s-2) 0 0;padding-left:18px;color:var(--hero-dim);
  font-size:var(--t-xs)}
.hero .nums{display:flex;flex-wrap:wrap;gap:var(--s-6);margin-top:var(--s-5);
  padding-top:var(--s-4);border-top:1px solid rgba(255,255,255,.16)}
.hero .nums div{display:flex;flex-direction:column;gap:2px}
.hero .nums .lbl{color:var(--hero-dim)}
.hero .nums b{font-family:var(--mono);font-size:var(--t-xl);font-weight:600;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.hero .nums small{color:var(--hero-dim);font-size:var(--t-3xs)}

/* Покрытие источников — рядом с вердиктом, тем же весом, что и цифры:
   вердикт, посчитанный по половине источников, обязан говорить об этом там же,
   где он объявлен. */
.coverage{margin:var(--s-4) 0 0;padding:var(--s-3) var(--s-4);border-radius:var(--r);
  background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.20);
  font-size:var(--t-sm);color:var(--hero-soft)}
.coverage.gap{border-color:#F0C765;color:#F5DCA0;
  background-image:repeating-linear-gradient(135deg,transparent 0 9px,
    rgba(240,199,101,.10) 9px 18px)}
.coverage b{font-weight:700}
.coverage a{color:inherit}
.coverage .miss{display:block;margin-top:var(--s-1);font-weight:700}

/* ---------------- выгрузка ---------------- */
.actions{display:flex;flex-wrap:wrap;gap:var(--s-2);margin-top:var(--s-4)}
.actions a,.actions button{display:inline-flex;align-items:center;gap:var(--s-2);
  min-height:40px;padding:var(--s-2) var(--s-4);border-radius:var(--r);
  font:inherit;font-size:var(--t-xs);font-weight:600;cursor:pointer;text-decoration:none;
  background:rgba(255,255,255,.10);color:var(--hero-ink);
  border:1px solid rgba(255,255,255,.28)}
.actions a:hover,.actions button:hover{background:rgba(255,255,255,.18)}
main > .actions{margin:0}
main > .actions a,main > .actions button{background:var(--surface);color:var(--ink-2);
  border-color:var(--line)}

.hint{color:var(--ink-2);font-size:var(--t-sm);line-height:1.5;margin:.35rem 0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);
  padding:var(--s-4) var(--s-5)}
.card > h1{margin:0 0 var(--s-2);font-size:var(--t-xl);letter-spacing:-.02em}
section.card{scroll-margin-top:22px}
section.card > h2{margin:0 0 var(--s-3);font-size:var(--t-2xs);font-weight:700;
  letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);
  display:flex;align-items:center;gap:var(--s-2);flex-wrap:wrap}

/* ---------------- шкала оценки ---------------- */
.gauge{display:flex;align-items:center;gap:var(--s-5);flex-wrap:wrap}
.gauge svg{flex:0 0 auto}
.gauge .val{font-family:var(--mono);font-size:var(--t-3xl);font-weight:600;
  letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1}
.gauge .cat{font-weight:600;margin-top:var(--s-1)}
.gauge .conf{min-width:170px;flex:1 1 170px}
.gauge.thin{opacity:.72}

/* ---------------- факты ----------------
   Линии рисуются щелями сетки, а не бордерами ячеек: при переносе на телефоне
   бордеры удваивались по краю и оставляли строки без разделителя. */
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  background:var(--surface);border:1px solid var(--line-soft);border-radius:var(--r);
  overflow:hidden}
/* Разделители — тенями по левому и верхнему краю ячейки: бордеры удваивались
   по краю сетки, а пустой хвост последнего ряда светился цветом линии. */
.fact{padding:var(--s-3);box-shadow:-1px 0 0 var(--line-soft),0 -1px 0 var(--line-soft)}
.fact .v{font-family:var(--mono);font-size:var(--t-xs);margin-top:2px;
  font-variant-numeric:tabular-nums;word-break:break-word}
.fact .v.hit{color:var(--crit);font-weight:600}
.fact .v.none{color:var(--ink-3)}
.fact .src{font-size:var(--t-3xs);color:var(--ink-3);margin-top:3px}
.fact details summary{cursor:pointer;font-size:var(--t-xs);color:var(--accent)}
.fact details .v{margin-top:var(--s-1)}

/* ---------------- таблицы ---------------- */
.scroll{overflow-x:auto;border:1px solid var(--line-soft);border-radius:var(--r)}
table{width:100%;border-collapse:collapse;font-size:var(--t-xs)}
th,td{text-align:left;padding:var(--s-2) var(--s-3);
  border-bottom:1px solid var(--line-soft);vertical-align:top}
th{font-size:var(--t-3xs);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);
  font-weight:700;background:var(--surface-2);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
td.r{text-align:right}
/* Именно background-color: у строк очереди фоном нарисована кромка вердикта,
   и короткая запись стёрла бы её при наведении. */
tbody tr:hover{background-color:var(--surface-2)}

/* На узком экране таблица разворачивается в карточки: горизонтальный скролл
   без подсказки уносил вправо колонку «Совпадение» — самое важное после суммы. */
@media(max-width:600px){
  .scroll{overflow-x:visible;border:0}
  /* Шапку прячем целиком: подпись каждого поля переехала в td::before. */
  table thead{display:none}
  table,tbody,tr,td{display:block;width:auto}
  tbody tr{background:var(--surface);border:1px solid var(--line-soft);
    border-radius:var(--r);padding:var(--s-3);margin-bottom:var(--s-2)}
  tbody tr:last-child{margin-bottom:0}
  td{display:flex;gap:var(--s-3);justify-content:space-between;align-items:baseline;
    border:0;padding:3px 0;text-align:left}
  td.r{text-align:left}
  td::before{content:attr(data-l);flex:0 0 auto;color:var(--ink-3);
    font-size:var(--t-3xs);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
  td:empty{display:none}
}

/* «Плашка»: номер производства, дела, ИНН, госномер. Отсылка к номерному знаку
   и одновременно подсказка, что это поле копируют. */
.plate{display:inline-block;font-family:var(--mono);font-variant-numeric:tabular-nums;
  border:1px solid var(--line);border-radius:3px;padding:1px 6px;background:var(--surface-2);
  letter-spacing:.01em}
button.copy{font:inherit;font-family:var(--mono);cursor:pointer;
  display:inline-flex;align-items:center;min-height:28px;
  border:1px solid var(--line);border-radius:3px;padding:2px 7px;
  background:var(--surface-2);color:inherit;letter-spacing:.01em}
button.copy:hover{border-color:var(--accent);color:var(--accent)}
button.copy[data-done="1"]{border-color:var(--good);color:var(--good)}

/* ---------------- состояния источника ----------------
   Форма несёт смысл раньше цвета: ответивший источник — сплошной чип,
   непроверенный — контурный со штриховкой. На чёрно-белой распечатке и при
   дальтонизме различие сохраняется. */
.tag{display:inline-flex;align-items:baseline;gap:5px;font-size:var(--t-3xs);
  font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:2px 7px;
  border-radius:3px;white-space:nowrap;border:1px solid transparent}
.tag .mark{font-family:var(--mono);font-weight:700;letter-spacing:0}
.tag.good{background:var(--good-bg);color:var(--good);border-color:var(--good-bg)}
.tag.warn{background:var(--warn-bg);color:var(--warn);border-color:var(--warn-bg)}
.tag.crit{background:var(--crit-bg);color:var(--crit);border-color:var(--crit-bg)}
.tag.mute{background:var(--surface-2);color:var(--ink-3);border-color:var(--line)}
.tag.accent{background:var(--accent-soft);color:var(--accent-ink);border-color:var(--accent-soft)}
.tag.plain{background:transparent;color:var(--ink-2);border-color:var(--line)}
/* Один класс на все четыре непроверенных состояния: серое «не подключено»
   читалось как «тут не на что смотреть», то есть ближе к «чисто». */
.tag.unchecked{background:transparent;color:var(--warn);border-color:var(--warn);
  border-style:dashed;
  background-image:repeating-linear-gradient(135deg,transparent 0 5px,
    var(--hatch) 5px 10px)}
/* «Источника нет» — тоже не ответ, но и не «не проверено»: рваная рамка со
   штриховкой обещала бы, что когда-нибудь сходим, а сюда сходить нельзя.
   Двойная сплошная рамка читается как закрытая дверь и переживает ч/б печать. */
.tag.nosource{background:transparent;color:var(--ink-2);border-color:var(--ink-2);
  border-style:double;border-width:3px;padding:0 5px}

.empty{color:var(--ink-2)}
.empty.unchecked{color:var(--warn);font-weight:600;
  border-left:3px solid var(--warn);padding-left:var(--s-3)}
.note{color:var(--ink-3);font-size:var(--t-2xs);margin:var(--s-2) 0 0}
.note.scope{border-left:2px solid var(--line);padding-left:var(--s-3)}

/* ---------------- источники ---------------- */
.sources{display:flex;flex-direction:column}
.srow{display:flex;gap:var(--s-3);align-items:baseline;padding:var(--s-2) 0;
  border-bottom:1px solid var(--line-soft);flex-wrap:wrap}
.srow:last-child{border-bottom:0}
.srow .nm{flex:0 0 168px;font-weight:600;font-size:var(--t-sm)}
@media(max-width:600px){.srow .nm{flex:1 1 100%}}
.srow .st{color:var(--ink-2);font-size:var(--t-xs)}

/* ---------------- факторы оценки ---------------- */
.factors{display:flex;flex-direction:column}
.frow{display:flex;gap:var(--s-3);align-items:baseline;padding:6px 0;
  border-bottom:1px solid var(--line-soft);font-size:var(--t-sm)}
.frow:last-child{border-bottom:0}
.frow .d{flex:0 0 46px;text-align:right;font-family:var(--mono);font-weight:700;
  font-variant-numeric:tabular-nums}
.frow.plus .d{color:var(--good)} .frow.minus .d{color:var(--crit)}

.meter{height:6px;border-radius:3px;background:var(--line-soft);overflow:hidden;
  margin:var(--s-2) 0 var(--s-1)}
.meter i{display:block;height:100%;border-radius:3px;background:var(--accent)}

footer{color:var(--ink-3);font-size:var(--t-2xs);max-width:78ch;padding:2px 2px 0}

/* ================ очередь ================
   Главный экран продукта: восемьсот должников и один вопрос — на кого тратить
   пошлину. Всё здесь подчинено ему. */

/* ---------------- ответ про деньги ----------------
   Один крупный счётчик вместо ряда равновеликих плиток: у плиток нет главной,
   а вопрос ровно один. Остальные цифры идут под ним и мельче. */
.hero .eyebrow{margin:0 0 var(--s-2);color:var(--hero-dim);font-family:var(--mono);
  font-size:var(--t-3xs);letter-spacing:.08em;text-transform:uppercase}
.hero.queue h1{font-size:var(--t-xl);color:var(--hero-soft);font-weight:600}
.answer{display:flex;align-items:baseline;gap:var(--s-3);flex-wrap:wrap;
  margin:var(--s-4) 0 0}
.answer .lbl{flex:0 0 100%;color:var(--hero-dim)}
.answer b{font-family:var(--mono);font-size:3.25rem;line-height:1;font-weight:600;
  letter-spacing:-.045em;font-variant-numeric:tabular-nums;color:var(--hero-ink)}
.answer .of{color:var(--hero-soft);font-size:var(--t-lg)}
@media(max-width:600px){.answer b{font-size:2.5rem}}

/* Сэкономленная пошлина — единственная хорошая новость на странице, и первая
   цифра, которую эта страница вообще показала владельцу. Отбита линией сверху
   и набрана как кредитовая строка сметы. */
.saved{margin:var(--s-5) 0 0;padding:var(--s-3) 0 0;
  border-top:1px solid rgba(255,255,255,.16);
  display:flex;align-items:baseline;gap:var(--s-3);flex-wrap:wrap}
.saved .lbl{flex:0 0 100%;color:#7BD7B0}
.saved b{font-family:var(--mono);font-size:var(--t-2xl);font-weight:600;
  letter-spacing:-.03em;font-variant-numeric:tabular-nums;color:#7BD7B0}
.saved small{color:var(--hero-dim);font-size:var(--t-2xs)}
.saved.flat .lbl,.saved.flat b{color:var(--hero-dim)}

/* ---------------- полоса пошлины ----------------
   Ширина сегмента — рубли на одной общей шкале. Строки без посчитанной
   пошлины ширины не получают: вместо них у полосы рваный правый край. */
.feebar{margin:var(--s-4) 0 0;padding:0}
.feebar .bar{display:flex;height:26px;border-radius:3px;overflow:hidden;
  background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.20)}
/* Рваный правый край: полоса заведомо не полна — часть строк без посчитанной
   пошлины, и ширины им взять неоткуда. */
.feebar .bar.open{border-right:2px dashed #F0C765;padding-right:7px;
  border-top-right-radius:0;border-bottom-right-radius:0}
.feebar .seg{display:block;min-width:3px;height:100%}
.feebar .seg.pay{background:#9CC2FF}
.feebar .seg.hold{background:repeating-linear-gradient(135deg,
  rgba(240,199,101,.85) 0 6px,rgba(240,199,101,.45) 6px 12px)}
.feebar .seg.save{background:#7BD7B0}
.feebar .keys{display:flex;flex-wrap:wrap;gap:var(--s-2) var(--s-5);margin:var(--s-3) 0 0}
.feebar .key{display:inline-flex;align-items:baseline;gap:6px;
  color:var(--hero-soft);font-size:var(--t-2xs)}
.feebar .key i{width:9px;height:9px;border-radius:2px;flex:0 0 auto;
  transform:translateY(1px)}
.feebar .key.pay i{background:#9CC2FF} .feebar .key.save i{background:#7BD7B0}
.feebar .key.hold i{background:#F0C765}
.feebar .key b{font-family:var(--mono);font-variant-numeric:tabular-nums;
  color:var(--hero-ink);font-size:var(--t-xs)}
.feebar .key .rows{color:var(--hero-dim)}
.feebar .tail{margin:var(--s-2) 0 0;color:#F5DCA0;font-size:var(--t-2xs)}
.feenone{margin:var(--s-4) 0 0;color:#F5DCA0;font-size:var(--t-sm)}

/* ---------------- прогон идёт ---------------- */
.running{background:var(--accent-soft);border:1px solid var(--accent);border-radius:var(--r);
  padding:var(--s-3) var(--s-4);color:var(--accent-ink);font-size:var(--t-sm)}
.running b{font-family:var(--mono);font-variant-numeric:tabular-nums}
.running p{margin:var(--s-2) 0 0;font-size:var(--t-2xs)}
.running .tick{color:var(--accent-ink);opacity:.75}
/* Зависший прогон перекрашивается: бодрая синяя плашка на оборванном прогоне
   врёт ровно так же, как «не проверено», поданное зелёным. */
.running.stalled{background:var(--warn-bg);border-color:var(--warn);color:var(--warn)}
.running.stalled .meter i{background:var(--warn)}
.running .halt{font-weight:600}
/* Единственная анимация на странице, и она означает ровно одно: работа идёт. */
.meter.live i{background-image:repeating-linear-gradient(135deg,
  rgba(255,255,255,.45) 0 6px,transparent 6px 12px);
  background-size:24px 100%;animation:crawl 1.1s linear infinite}
@keyframes crawl{to{background-position:24px 0}}

/* ---------------- полнота проверки ----------------
   Четыре состояния, которые обязаны сойтись в общее число. Раньше три из них
   сливались в бодрое «обработано N из M». */
.cover{display:flex;flex-direction:column}
.crow{display:grid;grid-template-columns:168px 1fr 64px;gap:var(--s-3);
  align-items:center;padding:var(--s-2) 0;border-bottom:1px solid var(--line-soft)}
.crow:last-child{border-bottom:0}
.crow .nm{font-weight:600}
.crow b{font-family:var(--mono);font-size:var(--t-lg);font-weight:600;text-align:right;
  font-variant-numeric:tabular-nums}
.crow small{grid-column:2/4;color:var(--ink-3);font-size:var(--t-2xs);margin-top:-4px}
.crow .track{height:8px;border-radius:2px;background:var(--line-soft);overflow:hidden}
.crow .track i{display:block;height:100%;background:var(--ink-3)}
.crow.full .track i{background:var(--good)}
.crow.partial .track i{background:repeating-linear-gradient(135deg,
  var(--warn) 0 4px,transparent 4px 8px)}
.crow.failed .track i{background:repeating-linear-gradient(135deg,
  var(--ink-3) 0 3px,transparent 3px 6px)}
.crow.pending .track i{background:var(--line)}
.crow.full b{color:var(--good)} .crow.partial b{color:var(--warn)}
@media(max-width:600px){.crow{grid-template-columns:1fr 64px}
  .crow .nm{grid-column:1/3} .crow .track{grid-column:1/2} .crow small{grid-column:1/3}}

/* ---------------- инструменты очереди ---------------- */
.tools{display:flex;flex-wrap:wrap;gap:var(--s-3) var(--s-5);margin:0 0 var(--s-3);
  align-items:end}
.tools p{margin:0;display:flex;flex-direction:column;gap:var(--s-1)}
.tools .find{flex:1 1 260px}
.tools input,.tools select{font:inherit;font-size:var(--t-sm);min-height:40px;
  padding:var(--s-2) var(--s-3);border:1px solid var(--line);border-radius:var(--r);
  background:var(--surface);color:var(--ink)}
.tools input{width:100%}
.filters{display:flex;gap:var(--s-2);flex-wrap:wrap;margin:0 0 var(--s-3)}
.filters button{font:inherit;font-size:var(--t-xs);font-weight:600;
  min-height:36px;padding:var(--s-2) var(--s-3);cursor:pointer;
  background:var(--surface);color:var(--ink-2);border:1px solid var(--line);border-radius:var(--r)}
.filters button[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
@media(max-width:860px){.filters button{min-height:44px;padding:10px 14px}}
.qstatus{margin:0 0 var(--s-2);color:var(--ink-3);font-size:var(--t-2xs)}
/* Шапка числовой колонки стоит над своими числами, а не над соседней. */
#money th:nth-child(2),#money th:nth-child(3),#money th:nth-child(4),
#queue th:nth-child(3),#queue th:nth-child(4){text-align:right}
/* Ширины заданы явно: при автоматической раскладке суммы и чипы вердикта
   растягивались по содержимому, а обоснование — то самое, ради чего строку
   читают, — сжималось в столбик по два слова. */
@media(min-width:601px){
  #queue table{table-layout:fixed}
  #queue th:nth-child(1){width:15%} #queue th:nth-child(2){width:17%}
  #queue th:nth-child(3){width:12%} #queue th:nth-child(4){width:11%}
  #queue th:nth-child(5){width:10%} #queue th:nth-child(6){width:35%}
  /* Название вердикта переносится, а не выезжает на соседнюю колонку: слова
     те же самые, что в боте и в выгрузке, и сокращать их здесь нельзя. */
  #queue td:first-child .tag{white-space:normal}
}
.note.thin{color:var(--warn);font-weight:600;border-left:3px solid var(--warn);
  padding-left:var(--s-3);margin-bottom:var(--s-2)}
.more{display:flex;justify-content:center;margin-top:var(--s-3)}
.more button{font:inherit;font-size:var(--t-xs);font-weight:600;min-height:44px;
  padding:var(--s-2) var(--s-6);cursor:pointer;background:var(--surface);
  color:var(--ink-2);border:1px solid var(--line);border-radius:var(--r)}
.more button:hover{border-color:var(--accent);color:var(--accent)}

/* ---------------- строки очереди ----------------
   Кромка слева несёт сразу два факта: цвет — вердикт, сплошная она или рваная —
   ответили ли все источники. Поэтому сортировка по баллу физически не может
   выглядеть надёжнее данных под ней. */
#queue tbody tr[data-tone]{--rail:var(--ink-3);
  background-image:linear-gradient(var(--rail),var(--rail));
  background-repeat:no-repeat;background-position:left top;background-size:3px 100%}
#queue tbody tr[data-tone="file"]{--rail:var(--good)}
#queue tbody tr[data-tone="order"]{--rail:var(--accent)}
#queue tbody tr[data-tone="review"]{--rail:var(--warn)}
#queue tbody tr[data-tone="drop"]{--rail:var(--crit)}
#queue tbody tr[data-cov="partial"]{background-image:repeating-linear-gradient(
  var(--rail) 0 6px,transparent 6px 11px)}
#queue tbody tr[data-cov="failed"]{background-image:repeating-linear-gradient(
  var(--rail) 0 3px,transparent 3px 6px)}
#queue tbody tr[data-tone] td:first-child{padding-left:var(--s-4)}
tbody tr.total td{background:var(--surface-2);font-weight:600;
  border-top:2px solid var(--line)}

/* Заголовок группы: сворачивается, считает свои строки и называет их сумму. */
tr[data-group] td{padding:0;background:var(--surface-2);
  border-bottom:1px solid var(--line)}
.ghead{display:flex;align-items:baseline;gap:var(--s-2);width:100%;font:inherit;
  font-size:var(--t-2xs);font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  padding:var(--s-2) var(--s-3);cursor:pointer;border:0;background:none;color:var(--ink-2);
  text-align:left}
.ghead:hover{color:var(--accent)}
.ghead .caret{width:0;height:0;flex:0 0 auto;border-left:5px solid currentColor;
  border-top:4px solid transparent;border-bottom:4px solid transparent;
  transform:rotate(90deg)}
.ghead[aria-expanded="false"] .caret{transform:none}
.ghead .gmoney{font-family:var(--mono);letter-spacing:0;color:var(--ink-3);
  text-transform:none;font-weight:600}

/* Должник и договор одной ячейкой: фамилия сверху, номер под ней кнопкой —
   номер копируют, фамилию нет. */
.who{display:block;font-weight:600}
td .who + button.copy{margin-top:3px;font-size:var(--t-3xs);min-height:24px}

/* Балл вместе со шкалой покрытия: число и то, по скольким источникам оно
   посчитано, стоят в одной ячейке и никогда не разъезжаются. */
.score{display:inline-flex;flex-direction:column;gap:1px;min-width:52px}
.score b{font-family:var(--mono);font-size:var(--t-sm);font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.1}
.score .track{display:block;width:46px;height:4px;border-radius:2px;
  background:var(--line-soft);overflow:hidden}
.score .track i{display:block;height:100%;background:var(--good)}
.score small{font-size:var(--t-3xs);color:var(--ink-3)}
.score.thin b{color:var(--warn)}
.score.thin .track i{background:repeating-linear-gradient(135deg,
  var(--warn) 0 3px,transparent 3px 6px)}
.score.none{color:var(--ink-3)}

/* Обоснование обрезано двумя строками: восемьсот абзацев подряд не читаются.
   Клик по строке раскрывает её, печать раскрывает всё. */
.why{display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden}
tr.open .why{-webkit-line-clamp:unset;line-clamp:unset;overflow:visible}
tbody tr[data-tone]{cursor:default}
@media(max-width:600px){
  /* Карточка на телефоне: вердикт и деньги — крупно, обоснование одной строкой
     до тапа. Полторы тысячи слов пояснений в списке из восьмисот карточек
     превращают его в стену текста. */
  #queue tbody tr[data-tone]{padding-left:var(--s-4)}
  /* Обоснование получает всю ширину карточки: подпись поля рядом с текстом
     съедала треть строки, и от объяснения вердикта оставалось два слова. */
  #queue td:last-child{display:block}
  #queue td:last-child::before{display:block;margin-bottom:2px}
  .why{-webkit-line-clamp:2;line-clamp:2}
  tr[data-group] td{padding:0;display:block}
  .score{flex-direction:row;align-items:baseline;gap:6px}
  .score .track{transform:translateY(-2px)}
}

@media(prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important}
  html{scroll-behavior:auto!important}
}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* Колонтитул печатной копии: на экране его нет. Объявлен до блока
   печати — правило с той же специфичностью ниже погасило бы его. */
.printfoot{display:none}

/* ---------------- печать ----------------
   Распечатку подшивают к делу, поэтому здесь не «скрыть лишнее», а вторая
   вёрстка того же документа. Тёмная тема в печать не попадает: цвета заданы
   литералами, а не переменными, которые переопределяет prefers-color-scheme. */
@page{size:A4;margin:16mm 13mm 18mm}
/* Номер страницы считается только полями @page: counter(page) больше нигде не
   доступен. Отдельным правилом, чтобы движок, который этих полей не знает, не
   выбросил заодно и размер листа. Дата формирования и подпись документа
   продублированы в .printfoot, которую браузер повторяет на каждом листе. */
@page{@bottom-right{content:"стр. " counter(page) " из " counter(pages);font-size:8pt}}
@media print{
  :root{
    --paper:#fff; --surface:#fff; --surface-2:#fff;
    --ink:#000; --ink-2:#1a1a1a; --ink-3:#3a3a3a;
    --line:#000; --line-soft:#7a7a7a;
    --good:#000; --warn:#000; --crit:#000; --accent:#000; --accent-ink:#000;
    --good-bg:#fff; --warn-bg:#fff; --crit-bg:#fff; --accent-soft:#fff;
    --hero:#fff; --hero-ink:#000; --hero-dim:#3a3a3a; --hero-soft:#1a1a1a;
  }
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  body{background:#fff;color:#000;font-size:10.5pt}
  nav,.filters,.actions,.copyhint,.tools,.qstatus,.more,.tick{display:none!important}
  .shell{display:block;max-width:none;padding:0}
  main{display:block}
  main > *{margin-bottom:5mm}
  .hero{background:#fff!important;color:#000!important;border:1.5pt solid #000;
    border-radius:0;padding:4mm}
  .hero h1{font-size:16pt} .hero .why{color:#000}
  .hero .sub,.hero .nums .lbl,.hero .nums small,.hero ul.reasons{color:#333}
  .hero .badge{background:#fff!important;border:1pt solid #000;color:#000!important}
  .hero .nums{border-top:.5pt solid #000}
  .coverage{background:#fff!important;border:1pt solid #000;color:#000}
  /* Цвета предупреждения заданы литералами и переживают печать: без явного
     сброса самая важная оговорка страницы уходит на бумагу бледно-жёлтой. */
  .coverage.gap{border-color:#000;color:#000;
    background-image:repeating-linear-gradient(135deg,transparent 0 9px,
    rgba(0,0,0,.10) 9px 18px)}
  .coverage.gap b,.coverage.gap a,.coverage.gap .miss{color:#000}
  .card{border:.5pt solid #000;border-radius:0;padding:3mm 4mm}
  .demo{border:1.5pt solid #000;color:#000;
    background-image:repeating-linear-gradient(135deg,transparent 0 10px,
      rgba(0,0,0,.12) 10px 20px)}
  /* Состояния источника на бумаге держатся на знаке, штриховке и рамке. */
  .tag{border:.5pt solid #000;background:#fff!important;color:#000!important}
  .tag.unchecked{border-style:dashed;
    background-image:repeating-linear-gradient(135deg,transparent 0 4px,
      rgba(0,0,0,.16) 4px 8px)!important}
  /* Без этой строки «источника нет» на бумаге стало бы обычным чипом и
     слилось бы с «проверено, записей нет» — ровно та подмена, ради снятия
     которой раздел заведён, и ровно на том листе, который подшивают. */
  .tag.nosource{border:2.5pt double #000}
  .empty.unchecked{border-left:2pt solid #000;color:#000}
  .plate,button.copy{border:.5pt solid #000;background:#fff}
  /* Таблицы не рвутся, шапка повторяется, заголовок не висит в конце листа. */
  .scroll{overflow:visible;border:.5pt solid #000;border-radius:0}
  table{font-size:9pt}
  thead{display:table-header-group}
  tr,.fact,.srow,.frow{break-inside:avoid;page-break-inside:avoid}
  section.card > h2,.card > h1{break-after:avoid;page-break-after:avoid}
  .hero,.demo,.coverage,.feebar,.crow{break-inside:avoid;page-break-inside:avoid}
  tbody tr:hover{background-color:transparent}
  /* ---- очередь на бумаге ----
     Всё, что на экране держалось на цвете, здесь держится на форме: сегмент
     полосы — заливкой, штриховкой или пустотой; кромка строки — сплошной или
     рваной; ни одна из этих разниц не исчезает на чёрно-белом принтере. */
  .hero.queue h1{color:#000}
  .answer b{color:#000} .answer .of{color:#1a1a1a}
  .saved{border-top:.5pt solid #000}
  .saved .lbl,.saved b{color:#000!important}
  .saved small{color:#333}
  .feebar .bar{border:.5pt solid #000;background:#fff}
  .feebar .bar.open{border-right:1.5pt dashed #000}
  .feebar .seg.pay{background:#000!important}
  .feebar .seg.hold{background-image:repeating-linear-gradient(135deg,
    #000 0 2px,#fff 2px 5px)!important;background-color:#fff!important}
  .feebar .seg.save{background:#fff!important;box-shadow:inset 0 0 0 .5pt #000}
  .feebar .key,.feebar .key b,.feebar .key .rows,.feebar .tail,.feenone{color:#000}
  .feebar .key i{border:.5pt solid #000}
  .feebar .key.pay i{background:#000} .feebar .key.save i{background:#fff}
  .feebar .key.hold i{background-image:repeating-linear-gradient(135deg,
    #000 0 2px,#fff 2px 4px)}
  .running{border:1pt solid #000;background:#fff!important;color:#000}
  .meter.live i{animation:none;background-image:none}
  .crow .track{border:.5pt solid #000}
  .crow.full .track i{background:#000}
  .crow.pending .track i{background:#fff}
  .crow b{color:#000!important}
  /* Кромка строки печатается: без неё лист теряет и вердикт, и полноту. */
  #queue tbody tr[data-tone]{background-size:2pt 100%}
  /* Свои доли колонок: на листе обоснование получает почти половину ширины.
     Автоматическая раскладка отдавала его двум словам в строку и растягивала
     восемьсот строк вдвое больше листов, чем нужно. */
  #queue table{table-layout:fixed;font-size:8pt}
  #queue th{white-space:normal}
  #queue th:nth-child(1){width:15%} #queue th:nth-child(2){width:17%}
  #queue th:nth-child(3){width:12%} #queue th:nth-child(4){width:11%}
  #queue th:nth-child(5){width:8%}  #queue th:nth-child(6){width:37%}
  /* Разрядка чипа съедала полколонки: на бумаге его читают не издалека. */
  #queue td:first-child .tag{font-size:6.5pt;letter-spacing:0;padding:1px 3px}
  #queue .plate,#queue button.copy{font-size:7.5pt;padding:0 3px}
  tr[data-group] td{background:#fff;border-bottom:1pt solid #000}
  .ghead{color:#000;padding:1.5mm 2mm}
  tbody tr.total td{background:#fff;border-top:1pt solid #000}
  .score b,.score small{color:#000}
  .score .track{border:.5pt solid #000}
  .score .track i{background:#000}
  /* Обрезанное обоснование на бумаге раскрывается целиком: строка, потерянная
     на листе, — потерянный факт. Это тот же принцип, что и с details. */
  .why{-webkit-line-clamp:unset!important;line-clamp:unset!important;
    display:block!important;overflow:visible!important}
  /* Свёрнутое раскрывается: скрытая на бумаге строка — это потерянный факт. */
  details{display:block} details summary{display:none}
  /* Внешние ссылки печатаются текстом. Адрес самой страницы сюда не попадает:
     в нём токен доступа, а лист уходит в дело. */
  a[href^="http"]::after{content:" (" attr(href) ")";font-size:8pt;word-break:break-all}
  a[href^="#"]{text-decoration:none;color:#000}
  /* Колонтитул: повторяется на каждом листе средствами браузера. */
  .printfoot{display:flex;position:fixed;bottom:0;left:0;right:0;
    gap:6mm;justify-content:space-between;
    border-top:.5pt solid #000;padding-top:1.5mm;font-size:8pt;color:#000}
}
"""
