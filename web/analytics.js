'use strict';
(() => {
  const el=id=>document.getElementById(`analytics-${id}`);
  let token='',active=false,data=null,version=0,loading=false,controller=null;
  const stateNames={met:'Минимум достигнут',open:'Неделя открыта',failed:'Не выполнено',unknown:'Недостаточно архива',excluded:'Не учитывается'};
  const node=(tag,value,cls)=>{const n=document.createElement(tag);n.textContent=value??'—';if(cls)n.className=cls;return n;};
  const number=n=>n===null||n===undefined?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:3}).format(n);
  const day=s=>String(s).slice(5).split('-').reverse().join('.');
  const date=s=>s?new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Yekaterinburg',dateStyle:'short',timeStyle:'short'}).format(new Date(typeof s==='number'?s*1000:s)):'—';
  const range=w=>`${day(w.start)}–${day(w.end)}`;
  function table(target,headings,rows){
    const wrap=el(target);wrap.replaceChildren();
    if(!rows.length){wrap.append(node('p','Нет данных','empty'));return;}
    const t=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');
    headings.forEach(h=>hr.append(node('th',h)));head.append(hr);t.append(head);
    const body=document.createElement('tbody');
    rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(v=>{const td=document.createElement('td');if(v instanceof Node)td.append(v);else td.textContent=v??'—';tr.append(td);});body.append(tr);});
    t.append(body);wrap.append(t);
  }
  function weeklyCell(w){const n=document.createElement('div');n.append(node('strong',w.posts),node('span',stateNames[w.status],`analytics-state ${w.status}`));return n;}
  function fillOptions(select,options){const old=select.value;select.replaceChildren(...options.map(([label,value])=>new Option(label,value)));if(options.some(x=>x[1]===old))select.value=old;}
  function renderPosts(){
    const g=data?.groups.find(g=>String(g.group_id)===el('community').value),week=el('post-week').value;
    if(!g){el('posts').replaceChildren(node('p','Выберите паблик или нажмите его название в таблице.','empty'));return;}
    const w=g.weeks.find(w=>w.start===week);
    const posts=g.publications.filter(p=>!w||(p.published_at>=Date.parse(w.start+'T00:00:00+05:00')/1000&&p.published_at<Date.parse(w.end_exclusive+'T00:00:00+05:00')/1000));
    table('posts',['Дата YEKT','Публикация','Тип','Доступность','Замер YEKT'],posts.map(p=>{
      const link=node('a',`Открыть №${p.post_id}`);link.href=p.url;link.target='_blank';link.rel='noopener noreferrer';
      return [date(p.published_at),link,p.is_repost?'Репост':'Публикация',p.status==='complete'?'Доступна':p.status==='missing_counters'?'Доступна, счётчики неполные':'Не возвращена API',date(p.measured_at)];
    }));
  }
  function renderGroups(){
    if(!data?.run)return;
    const query=el('search').value.trim().toLocaleLowerCase('ru-RU');
    const groups=data.groups.filter(g=>`${g.name} ${g.group_id}`.toLocaleLowerCase('ru-RU').includes(query));
    table('groups',['Паблик','Записей','L','K','R','V','Предв. ER, %','Предв. П3.3, %',...data.weekly_summary.map(range),'Архив'],groups.map(g=>{
      const name=node('button',g.name,'quiet analytics-group-link');
      name.onclick=()=>{el('community').value=String(g.group_id);renderPosts();el('community').focus();};
      const counters=['likes','comments','reposts','views'].map(k=>`${g.coverage[k]?number(g.known_sums[k]):'—'} (${g.coverage[k]}/${g.posts})`);
      const er=`${number(g.observed_er)} (${g.complete_counter_posts}/${g.posts})`;
      return [name,g.posts,...counters,er,number(g.observed_p33),...g.weeks.map(weeklyCell),g.business_card?'Страница-визитка':!g.included?'Исключён':g.archive_status==='complete'?'Полный':'Неполный'];
    }));
  }
  function render(){
    const runs=data.run&&!data.runs.some(r=>r.id===data.run.id)?[...data.runs,data.run]:data.runs;
    fillOptions(el('run'),[['Последний завершённый',''],...runs.map(r=>[`${date(r.started_at)} · ${r.posts_received} записей · ${r.status==='complete'?'полный':'неполный'}`,r.id])]);
    if(!data.run){
      el('asof').textContent='Сохранённых замеров пока нет.';el('cards').replaceChildren();
      ['week-summary','groups','posts'].forEach(id=>el(id).replaceChildren());el('caption').textContent='';
      fillOptions(el('community'),[['Выберите паблик','']]);fillOptions(el('post-week'),[['Весь период','']]);return;
    }
    const d=data.district,r=data.run;
    el('asof').textContent=`Замер ${date(r.started_at)} — ${date(r.finished_at)} YEKT. Публикации с ${r.period_start} по ${r.period_end} (конец не включён). Полнота: ${d.completeness==='complete'?'полная':'неполная'}. Это сохранённые данные, не текущее состояние стены VK.`;
    el('cards').replaceChildren();
    [[`${d.communities} паблика`,`${d.observed_available_posts} доступны · ${d.unavailable_known_posts} исторических недоступны`],
      [`ER по известным: ${number(d.observed_er)}%`,`${d.complete_counter_posts}/${d.posts} записей с полными счётчиками`],
      [`Предварительный П3.3: ${number(d.observed_p33)}%`,`Полный ER: ${number(d.er)}% · полный П3.3: ${number(d.p33)}%`]].forEach(([a,b])=>{const card=node('div','', 'analytics-card');card.append(node('strong',a),node('p',b,'muted'));el('cards').append(card);});
    el('caption').textContent=`ER = сумма лайков, комментариев и репостов / сумма просмотров × 100. Предварительный ER рассчитан только по записям с полными счётчиками; отсутствующие данные не равны нулю. L/K/R/V ниже — известные суммы. Репосты ${data.include_reposts?'учтены':'исключены'}. Границы недель: ${data.week_mode==='calendar'?'понедельник–воскресенье':'по 7 дней от начала периода'}.`;
    table('week-summary',['Неделя','Состояние на момент замера','Достигли ≥3','Не выполнено','Недостаточно архива','П3.2, %'],data.weekly_summary.map(w=>[range(w),w.closed?'Завершена':'Открыта',`${w.met}/${w.eligible}`,w.failed,w.unknown,number(w.p32)]));
    fillOptions(el('community'),[['Выберите паблик',''],...data.groups.map(g=>[g.name,String(g.group_id)])]);
    fillOptions(el('post-week'),[['Весь период',''],...data.weekly_summary.map(w=>[range(w),w.start])]);
    renderGroups();renderPosts();
  }
  async function load(){
    if(!token)return;
    controller?.abort();controller=new AbortController();const current=++version;loading=true;
    el('status').textContent='Чтение сохранённого замера…';el('status').className='muted';
    const params=new URLSearchParams({weeks:el('weeks').value,reposts:el('reposts').checked?'1':'0'});
    if(el('run').value)params.set('run',el('run').value);
    try{
      const response=await fetch('/api/analytics?'+params,{credentials:'same-origin',headers:{'X-CSRF-Token':token},signal:controller.signal});
      const body=await response.json();if(current!==version)return;
      if(response.status===401){clear();globalThis.dispatchEvent(new Event('analytics-login-required'));return;}
      if(!response.ok||body.error)throw new Error('analytics_unavailable');
      data=body;render();el('status').textContent='Данные прочитаны из аналитической базы VM. Сбор VK не запускался.';
    }catch(e){
      if(current!==version||e.name==='AbortError')return;
      el('status').textContent=data?'Не удалось обновить. Ниже остаются данные предыдущего успешного чтения; настройки отображённых данных указаны в подписи.':'Аналитика недоступна. Проверьте наличие сохранённого замера на VM и повторите чтение.';
      el('status').className='error';
    }finally{if(current===version)loading=false;}
  }
  function clear(){
    token='';active=false;data=null;version++;controller?.abort();loading=false;
    ['cards','week-summary','groups','posts'].forEach(id=>el(id).replaceChildren());
    el('asof').textContent='';el('caption').textContent='';el('status').textContent='Войдите в панель для чтения замеров.';
    fillOptions(el('run'),[['Последний завершённый','']]);fillOptions(el('community'),[['Выберите паблик','']]);fillOptions(el('post-week'),[['Весь период','']]);
  }
  el('refresh').onclick=load;['run','weeks','reposts'].forEach(id=>el(id).onchange=load);
  el('search').oninput=renderGroups;el('community').onchange=renderPosts;el('post-week').onchange=renderPosts;
  globalThis.VKAnalytics={setActive(on,csrf){active=on;if(on&&csrf){token=csrf;load();}},clear};
  globalThis.dispatchEvent(new Event('analytics-ready'));
  setInterval(()=>{if(active&&token&&!loading)load();},60000);
})();
