"use strict";
const $ = (s) => document.querySelector(s);
const money = (n) => new Intl.NumberFormat("ru-RU").format(n) + " ₽";
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const norm = (s) => s.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
const paths = {
  home:'<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  bag:'<path d="M5 8h14l2 13H3zM9 9V6a3 3 0 0 1 6 0v3"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  pin:'<path d="M19 9c0 5-7 12-7 12S5 14 5 9a7 7 0 1 1 14 0Z"/><circle cx="12" cy="9" r="2"/>',
  chevron:'<path d="m10 5 7 7-7 7"/>', back:'<path d="m14 5-7 7 7 7"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  edit:'<path d="m4 17 12-12 4 4L8 21H4zM14 7l4 4"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>'
};
const svg = (k) => '<svg aria-hidden="true" class="ic" viewBox="0 0 24 24">'+(paths[k] || icon(k))+'</svg>';
const S = {users:[], products:new Map(), bundles:[], orders:[], session:null,
  uid:localStorage.getItem("shop.user") || "katya",
  mode:localStorage.getItem("shop.mode") === "ordinary" ? "ordinary" : "personal",
  route:"home", draft:null, selected:null, modal:null, query:"", filter:"Все", receipt:null};
const current = () => S.users.find(u => u.id === S.uid);
const product = (sku) => S.products.get(sku);
const total = (items) => Object.entries(items).reduce((n,[sku,q])=>n+product(sku).price*q,0);
const vegetableShelf = "Овощи, грибы и зелень";
const batch = 40;
const photo = p => '<img src="assets/'+esc(p.image)+'.jpg?v=5" alt="" loading="lazy" decoding="async">';
const here = (part) => "/api/sessions/"+S.session.id+"/"+part;
const date = (s) => new Date(s).toLocaleString("ru-RU",{timeZone:"Europe/Moscow",day:"numeric",month:"long",hour:"2-digit",minute:"2-digit"});
const favKey = () => "shop.favorites."+S.uid;
const favorites = () => JSON.parse(localStorage.getItem(favKey()) || "[]");
const button = (action, label, cl="", id="") => '<button type="button" class="'+cl+'" data-action="'+action+'" data-id="'+esc(id)+'">'+label+'</button>';
const labeled = (action, label, symbol, cl, id="") => '<button type="button" aria-label="'+esc(label)+'" class="'+cl+'" data-action="'+action+'" data-id="'+esc(id)+'">'+symbol+'</button>';
async function api(url, method="GET", body) {
  if (window.pagesRequest) return window.pagesRequest(url, method, body);
  const res = await fetch(url, {method, headers: body ? {"Content-Type":"application/json"} : {},
    body:body ? JSON.stringify(body) : undefined, cache:"no-store"});
  if (!res.ok) {
    const payload = await res.json().catch(()=>({}));
    const error = new Error(typeof payload.detail === "string" ? payload.detail : "Не удалось выполнить действие. Повторите попытку.");
    error.status = res.status;
    throw error;
  }
  return res.json();
}
let toastTimer;
function notify(msg) {
  $("#toast").textContent=msg; $("#toast").classList.add("show");
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$("#toast").classList.remove("show"),3500);
}
// Each visible shopping interval has its own ID. Retried reports extend the same interval.
let visit=null, pendingVisit=Promise.resolve();
function reportVisit() {
  if(!visit) return pendingVisit;
  const body={visit_id:visit.key,elapsed_ms:Math.round(performance.now()-visit.start)};
  const url="/api/sessions/"+visit.sid+"/visits";
  pendingVisit=(window.pagesRequest ? api(url,"POST",body) : fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body),keepalive:true})).catch(()=>{});
  return pendingVisit;
}
function stopVisit() {
  const saved=reportVisit(); visit=null; return saved;
}
function syncVisit() {
  const measuring=S.session && !S.receipt && !document.hidden &&
    ["home","catalog","products","cart","recipe"].includes(S.route);
  if(visit && (!measuring || visit.sid!==S.session.id)) stopVisit();
  if(measuring && !visit) visit={key:crypto.randomUUID(),sid:S.session.id,start:performance.now()};
}
document.addEventListener("visibilitychange",syncVisit);
window.addEventListener("pagehide",stopVisit);
window.addEventListener("pageshow",event=>{
  if (event.persisted) location.reload();
  else syncVisit();
});
setInterval(reportVisit,15000);
async function enter(user, mode) {
  const session=await api("/api/users/"+user+"/sessions","POST",{mode});
  const [page,orders]=await Promise.all([
    mode === "personal" ? api("/api/sessions/"+session.id+"/recommendations/page") : {bundles:[],has_more:false,version:""},
    api("/api/users/"+user+"/orders")
  ]);
  Object.assign(S,{uid:user,mode,session,bundles:page.bundles,bundleVersion:page.version,moreBundles:page.has_more,orders,modal:null,selected:null,draft:null,
    filter:"Все",query:"",receipt:null});
  localStorage.setItem("shop.user",user); localStorage.setItem("shop.mode",mode);
}
function route(page, replace=false) {
  const allowed=["home","catalog","products","cart","profile","roles","recipe","checkout","thanks"];
  if (!allowed.includes(page) || (page==="recipe" && !S.draft) ||
      (page==="thanks" && !S.receipt) || (page==="checkout" && !Object.keys(S.session.items).length)) page="home";
  S.route=page; S.modal=null;
  const hash=page==="products" && S.filter!=="Все" ? "#products?"+new URLSearchParams({category:S.filter}) : "#"+page;
  if (location.hash!==hash) history[replace?"replaceState":"pushState"](null,"",hash);
  render(); syncVisit(); window.scrollTo(0,0);
}
function navBar() {
  if (!S.session) return "";
  if (S.route === "home" || S.route === "catalog") {
    const cartLabel = S.session.total ? money(S.session.total) : "";
    return '<nav class="home-dock" aria-label="Основная навигация">'+
      button("search",svg("search")+"Поиск","dock-search")+
      labeled("cart","Корзина · "+money(S.session.total),svg("bag")+cartLabel,
        "dock-cart"+(cartLabel?" filled":""))+'</nav>';
  }
  return '<nav class="bottom-nav" aria-label="Основная навигация">'+
    [["home","home","Главная"],["catalog","catalog","Каталог"],["cart","bag","Корзина"],["profile","user","Профиль"]].map(([p,i,t])=>
      button(p,svg(i)+'<span>'+t+(p==="cart" && S.session.total ? ' · '+money(S.session.total) : '')+'</span>',"tab "+(S.route===p?"active":""))).join("")+'</nav>';
}
function modes() {
  return '<div class="modes" role="group" aria-label="Режим приложения">'+[["ordinary","Обычный"],["personal","С наборами"]].map(([id,t])=>
    '<button data-action="mode" data-id="'+id+'" aria-pressed="'+(id===S.mode)+'">'+t+'</button>').join("")+'</div>';
}
function card(p) {
  const q=S.session.items[p.id] || 0;
  const saved=favorites().includes(p.id);
  const deal=p.available && p.old_price>p.price;
  return '<article class="catalog-card"><div class="card-photo">'+photo(p)+
    (deal?'<span class="sale">−'+Math.round((1-p.price/p.old_price)*100)+'%</span>':'')+
    labeled("favorite",saved?"Убрать из сохранённых":"Сохранить товар",svg("heart"),"fav "+(saved?"on":""),p.id)+'</div>'+
    '<div class="card-name">'+esc(p.name)+'</div><small>'+[esc(p.unit),p.illustrative_photo?"Фото для примера":""].filter(Boolean).join(" · ")+'</small>'+
    ( !p.available ? '<small>Цена уточняется</small>' : q?'<div class="card-step">'+labeled("less","Уменьшить количество","−","",p.id)+'<b>'+q+' шт.</b>'+labeled("more","Увеличить количество","+","",p.id)+'</div>':
    labeled("more","Добавить в корзину · "+p.name+" · "+money(p.price),
      (deal?'<del>'+p.old_price+'</del> ':'')+money(p.price)+'<b>＋</b>',"price-add",p.id))+'</article>';
}
function recCard(b,i) {
  const entries=Object.entries(b.items);
  return '<article class="bundle tone-'+i+'"><div class="bundle-body"><div class="bundle-title"><h3>'+esc(b.name)+'</h3><strong>'+money(b.total)+'</strong></div>'+
    '<div class="bundle-pics">'+entries.slice(0,4).map(([sku,q])=>'<div>'+photo(product(sku))+'<i>×'+q+'</i></div>').join("")+'</div>'+
    '<details class="ingredients"><summary>Состав набора · '+entries.length+' поз.</summary><ul>'+entries.map(([sku,q])=>'<li>'+esc(product(sku).name)+' <b>× '+q+'</b></li>').join("")+'</ul></details></div>'+
    '<div class="bundle-actions">'+button("bundle-add","Добавить в корзину","primary",b.id)+button("recipe","Изменить состав","secondary",b.id)+'</div></article>';
}
function collections(products) {
  const featured = ["sandwich","croissant","juice","snickers","greens","milk"];
  const tiles = featured.map(image => products.find(p => p.image === image)).filter(Boolean);
  return '<section class="collection-section"><h2>Подборки</h2>'+
    '<div class="collection-rail rail" aria-label="Подборки продуктов">'+tiles.map(p =>
      button("filter",'<span class="collection-photo">'+photo(p)+'</span><span>'+esc(p.category)+'</span>',
        "collection-tile",p.category)).join("")+'</div></section>';
}
function home() {
  const products = [...S.products.values()];
  const deals = products.filter(p => p.old_price > p.price);
  const links=[["catalog","catalog","Каталог"],["discount","percent","Скидки"],["new","new","Что нового"],["profile","repeat","Заказывали"],["favorites","heart","Сохранёнки"]];
  const hero = '<section class="storefront"><div class="storefront-top">'+
    '<div><strong>Гашека, 7</strong><small>Доставка от 15 минут</small></div>'+
    labeled("profile","Профиль · "+current().name,svg("user"),"hero-profile")+
    '</div><h1>'+button("filter","Что взять к завтраку? "+svg("chevron"),"story-link","К завтраку")+'</h1></section>';
  const shortcuts = '<nav class="shortcuts" aria-label="Разделы магазина">'+links.map(([a,i,t]) =>
    button(a,'<span class="nav-circle">'+svg(i)+'</span>'+t,"shortcut")).join("")+'</nav>';
  const offers = S.mode==="personal" ? '<section><div class="section-head"><h2>Собрали для вас</h2>'+
    (S.moreBundles ? button("refresh-bundles",svg("repeat")+"Обновить","refresh-bundles") : '<span class="rec-context">Все наборы просмотрены</span>')+'</div>'+
    '<div class="rec-context">По истории покупок · '+esc(current().name)+'</div>'+
    '<div class="bundles rail">'+S.bundles.map(recCard).join("")+'</div></section>' : "";
  const saleShelf = deals.length ? '<section><div class="section-head"><h2>Выгодная полка</h2>'+
    button("discount","Больше "+svg("chevron"),"shelf-more")+'</div><div class="deal-rail rail">'+
    button("discount",'<span>Все скидки<br>и акции</span><strong>до −'+Math.max(...deals.map(p=>Math.round(100*(1-p.price/p.old_price))))+'%</strong>',"sale-ticket")+
    deals.slice(0,6).map(card).join("")+'</div></section>' : "";
  return hero+'<div class="store-sheet">'+shortcuts+collections(products)+offers+saleShelf+
    '<section><div class="section-head"><h2>Каталог товаров</h2>'+
    button("filter","Все товары","text-btn","Все")+'</div>'+categories(false)+'<div class="cards">'+
    products.slice(0,8).map(card).join("")+'</div>'+button("catalog","Открыть весь каталог","catalog-all")+
    '</section><div class="home-mode"><span>Режим приложения</span>'+modes()+'</div></div>';
}
function categories(full) {
  const options=["Все",vegetableShelf,...new Set([...S.products.values()].map(p=>p.category))];
  if (full && !options.includes(S.filter)) options.push(S.filter);
  return '<div class="chips" aria-label="Категории">'+options.map(c=>button("filter",esc(c),"chip "+(S.filter===c?"chosen":""),c)).join("")+'</div>';
}
function matches(p,f) {
  if(f===vegetableShelf) return p.vegetable;
  if(f==="Скидки") return p.old_price>p.price;
  if(f==="Новинки") return p.new;
  if(f==="Сохранёнки") return favorites().includes(p.id);
  if(f==="К завтраку") return p.category==="Завтраки" || ["coffee","croissant","yogurt","milk"].includes(p.image);
  return f==="Все" || p.category===f || p.department===f;
}
function pageHead(title,back="home") {
  return '<div class="page-head">'+labeled(back,"Назад",svg("back"),"back white-back")+'<h1>'+esc(title)+'</h1></div>';
}
function searchBox(value,placeholder,id) {
  return '<label class="find">'+svg("search")+'<input id="'+id+'" autocomplete="off" type="search" aria-label="'+placeholder+'" placeholder="'+placeholder+'" value="'+esc(value)+'"></label>';
}
function searchProducts(query, predicate) {
  const words=norm(query).split(/\s+/).filter(Boolean);
  const list=[...S.products.values()].filter(p=>predicate(p) && words.every(w=>p.searchText.includes(w)));
  if(words.length) {
    const rank=p=>words[0]==="овощи" && p.vegetable?-1:norm(p.name).startsWith(words[0])?0:norm(p.name).includes(words[0])?1:p.vegetable?2:3;
    list.sort((a,b)=>rank(a)-rank(b));
  }
  return list;
}
function moreButton(count,limit,action) {
  return count>limit ? button(action,"Показать ещё","catalog-all") : "";
}
function results() {
  const found=searchProducts(S.query,p=>matches(p,S.filter));
  if(!S.query && S.filter===vegetableShelf) found.sort((a,b)=>Number(!!b.image)-Number(!!a.image));
  return found.length?'<p class="rec-context">Найдено товаров · '+found.length+'</p><div class="cards">'+
    found.slice(0,S.limit).map(card).join("")+'</div>'+moreButton(found.length,S.limit,"more-results"):
    '<p class="noresults">'+(S.filter==="Сохранёнки"?"Здесь появятся товары, которые вы отметите сердечком.":"Ничего не нашлось. Попробуйте другое название или категорию.")+'</p>';
}
function catalogPage() {
  const products = [...S.products.values()];
  const tiles = new Map();
  for (const p of products) {
    if (!tiles.has(p.category)) tiles.set(p.category,p);
  }
  const tile = (label,p) => button("filter",'<span>'+esc(label)+'</span>'+photo(p),"category-tile",label);
  const featured = [[vegetableShelf,"carrot"],["Скидки","croissant"],["К завтраку","syrniki"],["Новинки","sandwich"]];
  return pageHead("Каталог")+'<div class="category-grid">'+featured.map(([label,image]) =>
    tile(label,products.find(p=>p.image===image))).join("")+'</div>'+
    '<section class="department-list"><div class="section-head"><h2>Продукты</h2>'+
    button("filter","Все товары","shelf-more","Все")+'</div><div class="category-grid">'+
    [...tiles].map(([label,p])=>tile(label,p)).join("")+'</div></section>';
}
function productsPage() {
  return pageHead(S.filter==="Все"?"Все товары":S.filter,"catalog")+searchBox(S.query,"Найти продукты","catalog-search")+categories(true)+'<div id="results">'+results()+'</div>';
}
function row(sku,q) {
  const p=product(sku);
  return '<div class="product-row"><div class="prod-img">'+photo(p)+'</div><div class="prod-content"><div class="prod-name">'+esc(p.name)+'</div><div class="prod-unit">'+esc(p.unit)+' · '+money(p.price)+' за уп.</div>'+
    '<div class="prod-row-bottom">'+button("replace","Заменить","replace-btn",sku)+
    '<div class="quantity">'+labeled("row-less","Уменьшить количество","−","",sku)+'<span>'+q+'</span>'+labeled("row-more","Увеличить количество","+","",sku)+'</div>'+
    labeled("remove","Убрать товар","×","remove-btn",sku)+'</div></div><div class="prod-price">'+money(p.price*q)+'</div></div>';
}
function basketPage(editor) {
  const items=editor?S.draft:S.session.items, filled=Object.keys(items).length>0;
  return '<section class="narrow">'+pageHead(editor?S.selected.name:"Корзина")+
    '<div class="rec-context">'+(editor?"Измените продукты или количество упаковок":esc(current().name)+" · "+(S.mode==="ordinary"?"Обычный режим":"С наборами"))+'</div>'+
    (filled?Object.entries(items).map(([k,q])=>row(k,q)).join(""):'<div class="empty">'+svg("bag")+'<h3>'+(editor?"В наборе пока пусто":"Корзина пуста")+'</h3></div>')+
    button(editor?"pick":"catalog","＋ Добавить продукты","add-product")+
    '<div class="summary"><div class="total-line">Итого <b>'+money(total(items))+'</b></div>'+
    '<button '+(filled?'':'disabled')+' data-action="'+(editor?"draft-add":"checkout")+'" class="checkout-cta">'+(editor?"Добавить в корзину":"К оформлению")+'</button></div></section>';
}
function checkoutPage() {
  return '<section class="narrow">'+pageHead("Оформление заказа","cart")+'<div class="pay-card"><b>Демонстрационный заказ</b><span>Сохраним покупки в истории профиля. Оплата и доставка отключены.</span></div>'+
    Object.entries(S.session.items).map(([k,q])=>'<div class="preview-line"><span>'+esc(product(k).name)+' × '+q+'</span><b>'+money(product(k).price*q)+'</b></div>').join("")+
    '<div class="summary"><div class="total-line">Итого <b>'+money(S.session.total)+'</b></div>'+button("place","Оформить демозаказ","checkout-cta")+'</div></section>';
}
function profilePage() {
  return '<section class="narrow">'+pageHead("Профиль")+ '<div class="person"><span class="avatar">'+esc(current().name[0])+'</span><div><h2>'+esc(current().name)+'</h2><p>'+esc(current().occupation)+'</p></div>'+
    button("roles","Сменить","secondary")+'</div><div class="section-head"><h3>Режим приложения</h3>'+modes()+'</div>'+(window.pagesRequest ? button('download-metrics','Скачать журнал эксперимента','secondary') : '')+'<h2>История заказов</h2>'+
    S.orders.map(o=>'<details class="order"><summary><span>'+date(o.ordered_at)+'<small>'+Object.keys(o.items).length+' поз. · '+(o.demo_seed?'Из демоистории':'Оформлен')+'</small></span><b>'+money(o.total_rub)+'</b></summary>'+
      Object.entries(o.items).map(([k,q])=>'<div class="preview-line"><span>'+esc(product(k).name)+' × '+q+'</span><strong>'+money(o.prices[k]*q)+'</strong></div>').join("")+'</details>').join("")+'</section>';
}
function roles() {
  const cards = S.users.map(u=>button("switch-user",'<span class="avatar">'+esc(u.name[0])+'</span><span><div class="who">'+esc(u.name)+'</div><div class="position">'+esc(u.occupation)+'</div><div class="habit">'+esc(u.habit)+'</div></span><span class="arrow">›</span>',"role-card",u.id)).join("");
  if (!S.session) {
    return '<dialog id="entry-profile" aria-labelledby="entry-title"><div class="dialog-head"><h2 id="entry-title">Выбор профиля</h2></div>'+cards+'</dialog>';
  }
  return '<section class="narrow">'+pageHead("Выберите профиль","profile")+cards+'</section>';
}
function pickerRows() {
  const list=searchProducts(S.modal.query,p=>p.available && p.id!==S.modal.sku);
  return list.slice(0,S.modal.limit).map(p=>'<div class="select-row"><div class="select-emoji">'+photo(p)+'</div><div class="select-title">'+esc(p.name)+'<small>'+esc(p.unit)+' · '+money(p.price)+'</small></div>'+button("select","Выбрать","select-choose",p.id)+'</div>').join("")+moreButton(list.length,S.modal.limit,"more-picker") || '<p class="noresults">Ничего не нашлось</p>';
}
function picker() {
  return '<dialog id="picker" aria-labelledby="picker-title"><div class="dialog-head"><h2 id="picker-title">'+(S.modal.sku?"Заменить товар":"Добавить товар")+'</h2>'+labeled("close","Закрыть",svg("close"),"back white-back")+'</div>'+
    searchBox(S.modal.query,"Поиск по каталогу","picker-search")+'<div class="picker-list" id="picker-list">'+pickerRows()+'</div></dialog>';
}
function thanks() {
  return '<section class="thanks"><div class="success-icon">✓</div><h1>Заказ оформлен</h1><p>Покупка на '+money(S.receipt.total_rub)+' сохранена в истории.</p>'+button("profile","Посмотреть заказ","primary")+button("home","На главную","secondary")+'</section>';
}
const pages={home,catalog:catalogPage,products:productsPage,cart:()=>basketPage(false),recipe:()=>basketPage(true),
  checkout:checkoutPage,thanks,roles,profile:profilePage};
function render() {
  const shop = $("#shop");
  const scroll = shop.dataset.route === S.route
    ? [...shop.querySelectorAll(".rail")].map(el => el.scrollLeft) : [];
  shop.dataset.route = S.route;
  shop.innerHTML='<main class="page">'+pages[S.route]()+'</main>'+navBar()+(S.modal?picker():"");
  shop.querySelectorAll(".rail").forEach((el,i) => { el.scrollLeft=scroll[i] || 0; });
  if (!S.session) $("#entry-profile").showModal();
  if (S.modal) $("#picker").showModal();
}
function setQty(items,sku,q) {
  if(q>999) throw new Error("Максимум 999 упаковок одного товара");
  if(q>0) items[sku]=q; else delete items[sku];
}
async function update(items,offer=null) {
  S.session=await api(here("cart"),"PATCH",{revision:S.session.revision,items,offer});
}
async function edit(before,after,action) {
  await api(here("edits"),"POST",{offer:S.selected.id,before,after,action});
  S.draft=after;
}
async function run(a,id) {
  if (a === "download-metrics") {
    const events = await api("/api/metrics");
    const file = new Blob([JSON.stringify(events, null, 2)], {type:"application/json"});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(file);
    link.download = "samokat-experiment.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    return;
  }
  if (S.receipt && a!=="place") {
    await enter(S.uid,S.mode);
  }
  if(a==="mode" || a==="switch-user") {
    if(a==="mode" && id===S.mode) return;
    const firstEntry = !S.session;
    const dest=S.route==="profile" && a==="mode"?"profile":"home";
    await enter(a==="switch-user"?id:S.uid,a==="mode"?id:S.mode); return route(firstEntry && location.hash !== "#roles" ? readRoute() : dest, firstEntry);
  }
  if(a==="refresh-bundles") {
    const control=$('[data-action="refresh-bundles"]');
    control.disabled=true;
    control.textContent="Обновляем…";
    try {
      const page=await api(here("recommendations/refresh"),"POST",{version:S.bundleVersion});
      Object.assign(S,{bundles:page.bundles,bundleVersion:page.version,moreBundles:page.has_more});
      render();
      const shelf=$(".bundles");
      if(shelf) shelf.scrollLeft=0;
      $('[data-action="refresh-bundles"]')?.focus({preventScroll:true});
      notify("Наборы обновлены");
    } finally {
      control.disabled=false;
      control.innerHTML=svg("repeat")+"Обновить";
    }
    return;
  }
  if(a==="more-results") {S.limit+=batch; $("#results").innerHTML=results(); return;}
  if(a==="more-picker") {S.modal.limit+=batch; $("#picker-list").innerHTML=pickerRows(); return;}
  if(a==="recipe" || a==="bundle-add" || a==="draft-add") {
    const b=a==="draft-add"?S.selected:S.bundles.find(x=>x.id===id);
    if(!b) throw new Error("Откройте набор ещё раз");
    if(a==="recipe") {S.selected=b; S.draft={...b.items}; return route("recipe");}
    await update(a==="draft-add"?S.draft:b.items,b.id);
    notify("Набор добавлен в корзину"); return route("cart");
  }
  if(a==="place") {
    await stopVisit();
    S.receipt=await api(here("checkout"),"POST",{revision:S.session.revision});
    S.session={...S.session,items:{},total:0}; return route("thanks");
  }
  if(["less","more","row-less","row-more","remove"].includes(a)) {
    const editor=S.route==="recipe", before=editor?S.draft:S.session.items, after={...before};
    const q=(after[id]||0)+(["less","row-less"].includes(a)?-1:1);
    setQty(after,id,a==="remove"?0:q);
    if(editor) await edit(before,after,after[id]?"quantity":"remove"); else await update(after);
    if(a==="more" && !before[id]) notify("Товар в корзине");
    return render();
  }
  if(a==="replace" || a==="pick") {S.modal={sku:a==="replace"?id:null,query:"",limit:batch}; return render();}
  if(a==="close") {S.modal=null; return render();}
  if(a==="select") {
    const editor=S.route==="recipe",before=editor?S.draft:S.session.items, after={...before};
    const old=S.modal.sku, qty=old?after[old]:1;
    if(old) delete after[old]; setQty(after,id,(after[id]||0)+qty);
    if(editor) await edit(before,after,old?"replace":"add"); else await update(after);
    S.modal=null; return render();
  }
  if(a==="favorite") {
    const fav=new Set(favorites()); if(fav.has(id)) fav.delete(id); else fav.add(id);
    localStorage.setItem(favKey(),JSON.stringify([...fav])); return render();
  }
  const filters={discount:"Скидки",new:"Новинки",favorites:"Сохранёнки",search:"Все"};
  if(a==="filter" || Object.hasOwn(filters,a)) {
    S.filter=a==="filter"?id:filters[a]; if(a!=="filter" || S.route!=="products") S.query="";
    S.limit=batch; route("products"); if(a==="search") $("#catalog-search").focus(); return;
  }
  if(a==="profile") S.orders=await api("/api/users/"+S.uid+"/orders");
  if(a==="home") S.filter="Все";
  if(pages[a]) route(a);
}
let busy=false;
async function guarded(fn) {
  if(busy) return;
  busy=true; $("#shop").setAttribute("aria-busy","true");
  try { await fn(); } catch(e) {
    if(e.status===409) {
      try {await enter(S.uid,S.mode); route("cart",true);} catch {}
    }
    notify(e.message || "Проверьте соединение");
  } finally {busy=false; $("#shop").removeAttribute("aria-busy");}
}
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-action]");
  if(b && !b.disabled) guarded(()=>run(b.dataset.action,b.dataset.id));
  else if(e.target.id==="picker" && e.target===e.currentTarget?.activeElement) {
    const r=e.target.getBoundingClientRect();
    if(e.clientX<r.left || e.clientX>r.right || e.clientY<r.top || e.clientY>r.bottom) guarded(()=>run("close"));
  }
});
document.addEventListener("cancel",e=>{if(e.target.id==="entry-profile"){e.preventDefault(); return;} if(e.target.id==="picker"){e.preventDefault(); guarded(()=>run("close"));}},true);
document.addEventListener("input",e=>{
  if(e.target.id==="catalog-search"){S.limit=batch; S.query=e.target.value; $("#results").innerHTML=results();}
  if(e.target.id==="picker-search"){S.modal.limit=batch; S.modal.query=e.target.value; $("#picker-list").innerHTML=pickerRows();}
});
function readRoute() {
  const [page,query]=location.hash.slice(1).split("?");
  if(page==="products") {
    S.filter=new URLSearchParams(query).get("category") || "Все";
    S.query="";
    S.limit=batch;
  }
  return page || "home";
}
window.addEventListener("popstate",()=>guarded(async()=>{
  if (!S.session) return;
  const page=readRoute();
  if(S.receipt && page!=="thanks") await enter(S.uid,S.mode);
  route(page,true);
}));
async function init() {
  const [users,products]=await Promise.all([api("/api/users"),api("/api/catalog")]);
  S.limit=batch;
  for(const p of products) p.searchText=norm([p.name,p.category,p.department,p.group,...p.sections].join(" "));
  S.users=users; S.products=new Map(products.map(p=>[p.id,p]));
  S.route="roles";
  render();
}
init().catch(()=>{$("#shop").innerHTML='<div class="loading"><h2>Магазин временно недоступен</h2><p>Обновите страницу и проверьте, разрешено ли браузеру сохранять данные сайта.</p><button class="primary" onclick="location.reload()">Попробовать снова</button></div>';});
