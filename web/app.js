'use strict';
const $ = id => document.getElementById(id);
let csrf = '', snapshot = null, busy = false, activeTab = 'overview';
const errors = {login_required:'Войдите в панель.',invalid_credentials:'Неверный логин или пароль.',login_throttled:'Слишком много попыток входа. Подождите 10 минут.',account_must_be_disabled_for_login:'Перед началом входа отключите аккаунт в настройках.',account_authentication_required:'Нужна свежая проверка нужного VK ID. Автоматика остаётся отключённой.',profile_busy:'Профиль занят. Дождитесь завершения текущей операции; активное окно не закрывается.',account_guard:'Привязка аккаунта не прошла проверку. Действия не разрешены.',backend_unavailable:'Backend Windows VM недоступен. Данные не обновлены.',backend_invalid_response:'Backend вернул некорректный ответ.',backend_timeout_outcome_unknown:'Backend не ответил вовремя. Исход команды неизвестен; повторять её автоматически нельзя.',invalid_csrf:'Сессия устарела. Обновите страницу.',invalid_origin:'Источник запроса отклонён.',invalid_operation_or_fields:'Проверьте введённые настройки.'};
function message(error) { return errors[error] || `Операция отклонена: ${error || 'ошибка сети'}`; }
errors.community_destinations_required='Для репостов в сообщества укажите два различных ID групп через запятую.';
errors.account_schedule_capacity='Суммарные квоты аккаунтов не помещаются в настроенное рабочее окно с текущими паузами. Прежние настройки сохранены.';
function text(tag, value, cls) {const node=document.createElement(tag);node.textContent=value ?? '—';if(cls)node.className=cls;return node;}
function value(number){return number===null||number===undefined?'—':number;}
// BEGIN schedule window helpers
function workWindowLabel(window) {
  if (!Number.isInteger(window?.start_hour) || !Number.isInteger(window?.end_hour) ||
      window.start_hour < 9 || window.end_hour > 22 || window.start_hour >= window.end_hour) return null;
  return `${String(window.start_hour).padStart(2,'0')}:00–${String(window.end_hour).padStart(2,'0')}:00 YEKT`;
}
function scheduleWindowText(scheduler) {
  const current=workWindowLabel(scheduler?.work_window);
  const pending=workWindowLabel(scheduler?.pending_work_window);
  if (!current) return 'Рабочее окно уточняется';
  return pending && pending !== current
    ? `Сегодня ${current} · с ${scheduler.policy_effective_day || 'первого нового дня'}: ${pending}`
    : current;
}
// END schedule window helpers
// BEGIN schedule batch helpers
function scheduleBatchText(scheduler) {
  const label=(size,count)=>Number.isInteger(size)&&size>0&&size<=6&&Number.isInteger(count)&&count>0
    ? `до ${size} попыток в пачке, пачек за день: ${count}` : null;
  const current=label(scheduler?.batch_size,scheduler?.batch_count);
  const pending=label(scheduler?.pending_batch_size,scheduler?.pending_batch_count);
  if(!current)return 'Размер пачек уточняется';
  return `Сегодня: ${current}`+(pending&&pending!==current
    ? ` · с ${scheduler.policy_effective_day||'первого нового дня'}: ${pending}` : '');
}
// END schedule batch helpers
// BEGIN heartbeat helpers
function workerHeartbeat(input, now = Date.now()) {
  const heartbeat = input && typeof input === 'object' ? input : {at: input};
  const rawAt = heartbeat.at;
  const parsedAt = typeof rawAt === 'string' ? Date.parse(rawAt) : NaN;
  const validAt = Number.isFinite(parsedAt);
  // A stale heartbeat cannot establish that a process has stopped. Only the
  // explicit saved stopped phase is labelled stopped; freshness stays separate.
  const stale = validAt && now - parsedAt > 180000;
  const phase = typeof heartbeat.phase === 'string' ? heartbeat.phase : '';
  const phases = {
    stopped: 'Остановлен',
    waiting_window: 'Ожидание настроенного рабочего окна',
    waiting_batch: 'Ожидание следующей пачки',
    monitoring: 'Работа по расписанию',
    waiting_profile_lock: 'Ожидание освобождения профиля',
    refreshing_counters: 'Обновление счётчиков',
    executing_actions: 'Выполнение действий пачки',
    dry_run_monitoring: 'Мониторинг без отправки действий',
    preview: 'Предпросмотр расписания',
    attention: 'Требуется проверка защитной остановки',
    technical_error_backoff: 'Пауза после технического сбоя',
    batch_complete: 'Пачка завершена',
    batch_spacing_wait: 'Ожидание минимального интервала между пачками',
    batch_window_or_budget_closed: 'Пачка завершена досрочно: старый код не различал причину',
    batch_action_deadline: 'Достигнута граница действий: оставлена минута до следующей пачки',
    batch_work_window_closed: 'Закрыто настроенное рабочее окно',
    batch_daily_quota_reached: 'Достигнута дневная квота действий',
    batch_stop_requested: 'Запрошена остановка worker',
    batch_account_disabled: 'Аккаунт отключён до действия',
    batch_day_changed: 'Наступил другой календарный день YEKT',
    batch_spacing_deadline: 'Пропущено: интервал 9 мин не помещается до границы следующей пачки',
    batch_guard_rejected: 'Действие отклонено защитной проверкой; причина не уточнена',
    counter_refresh_budget: 'Бюджет чтения исчерпан',
    reserve_counter_refresh_budget: 'Пачка выполнена частично: исчерпан бюджет обновления резерва',
    reserve_counter_refresh_empty: 'Пачка выполнена частично: нет свежих подходящих записей в резерве',
    reserve_counter_refresh_stopped: 'Пачка выполнена частично: обновление резерва остановлено',
    counter_refresh_deadline: 'Обновление счётчиков отложено до следующей пачки',
    reserve_counter_refresh_deadline: 'Пачка выполнена частично: оставлено время для следующей пачки',
    reserve_waiting_profile_lock: 'Пачка выполнена частично: профиль занят при обновлении резерва',
    queue_empty: 'Подходящих заданий нет'
  };
  const modes = {live: 'реальные действия (live)', dry_run: 'проверка без отправки (dry_run)', preview: 'предпросмотр'};
  const state = phase === 'stopped' ? 'stopped' : !validAt ? 'unknown' : stale ? 'stale' : 'fresh';
  let phaseText = phases[phase] || (phase ? `Неизвестный этап (${phase})` : 'неизвестен');
  // Recalculate from saved instants on the cached render timer. A stale signal
  // cannot confirm a continuing wait or action, even when its deadline is valid.
  if (state === 'fresh' && parsedAt <= now + 5000) {
    const duration = seconds => `${Math.floor(seconds / 60)} мин ${String(seconds % 60).padStart(2, '0')} с`;
    const instant = raw => typeof raw === 'string' ? Date.parse(raw) : NaN;
    const yekt = timestamp => new Intl.DateTimeFormat('ru-RU', {timeZone:'Asia/Yekaterinburg', hour:'2-digit', minute:'2-digit', second:'2-digit'}).format(new Date(timestamp)) + ' YEKT';
    if (phase === 'refreshing_counters') {
      const read = heartbeat.counter_refresh || {};
      if (Number.isInteger(read.read_attempt) && read.read_attempt >= 2 && read.read_attempt <= 3 && read.read_attempt_limit === 3) {
        phaseText += ` · Повтор чтения ${read.read_attempt}/${read.read_attempt_limit}`;
        const readyAt = instant(read.read_retry_wait_until);
        if (Number.isFinite(readyAt) && readyAt > now) {
          phaseText += ` · до ${yekt(readyAt)} · осталось ${duration(Math.ceil((readyAt - now) / 1000))}`;
        }
      }
    }
    if (phase === 'batch_spacing_wait') {
      const readyAt = instant(heartbeat.spacing_ready_at);
      const savedRemaining = heartbeat.spacing_remaining_seconds;
      const remaining = Number.isFinite(readyAt) ? Math.max(0, Math.ceil((readyAt - now) / 1000))
        : typeof savedRemaining === 'number' && Number.isFinite(savedRemaining) && savedRemaining >= 0
          ? Math.max(0, Math.ceil(savedRemaining - Math.max(0, now - parsedAt) / 1000)) : null;
      if (remaining !== null) phaseText += `${Number.isFinite(readyAt) ? ` · до ${yekt(readyAt)}` : ''} · ${remaining > 0 ? `осталось ${duration(remaining)}` : 'минимальный интервал истёк; ожидается новый сигнал worker'}`;
    }
    const deadline = instant(heartbeat.batch_deadline_at);
    if (Number.isFinite(deadline)) {
      const remaining = Math.max(0, Math.ceil((deadline - now) / 1000));
      phaseText += ` · Граница действий ${yekt(deadline)}: ${remaining > 0 ? `осталось ${duration(remaining)}` : 'достигнута'}`;
    }
  }
  return {
    at: validAt ? rawAt : null,
    state,
    stateText: {
      stopped: 'Остановлен — последний сохранённый статус worker',
      unknown: 'Нет достоверного времени сигнала; состояние worker неизвестно',
      stale: 'Сигнал старше 3 минут; текущая работа worker не подтверждена',
      fresh: 'Свежий сигнал worker'
    }[state],
    freshnessText: !validAt ? 'время неизвестно' : stale ? 'старше 3 минут' : 'не старше 3 минут',
    phaseText,
    modeText: modes[heartbeat.mode] || 'неизвестен'
  };
}
// END heartbeat helpers
function accountAuthentication(account) {
  const proof = account?.authentication;
  let state = proof?.state || 'unverified';
  const expected = proof?.expected_vk_id ?? account?.vk_id ?? null;
  const observed = proof?.observed_vk_id ?? null;
  const hasIdentity = Number.isSafeInteger(expected) && expected > 0 && Number.isSafeInteger(observed) && observed > 0;
  if (state === 'matched') {
    if (hasIdentity && expected !== observed) state = 'wrong_account';
    else if (!hasIdentity) state = 'unverified';
    else if (proof.fresh !== true) state = 'stale';
  }
  const labels = {
    matched:'Вход подтверждён', wrong_account:'Открыт другой аккаунт',
    not_authenticated:'Вход в VK не подтверждён', unverified:'Вход не проверен',
    stale:'Проверка входа устарела'
  };
  const newIdentity = state === 'unverified' && expected === null && Number.isSafeInteger(observed) && proof?.fresh === true && proof?.can_finish === true;
  return {state, label:newIdentity ? 'Вход обнаружен; завершите привязку' : labels[state] || labels.unverified, expected, observed,
    checkedAt:proof?.checked_at || null,
    canEnable:state === 'matched' && proof?.fresh === true && !['opening','waiting_browser','awaiting_login','ready','wrong_account'].includes(account?.login?.state),
    canFinish:proof?.can_finish === true && proof?.fresh === true && account?.login?.state === 'ready' && account?.login?.verification_version === 2};
}
function cachedMetric(group, metric) {
  const total = Number.isInteger(group.posts) && group.posts >= 0 ? group.posts : 0;
  const known = group[metric + '_known_posts'];
  const sum = group[metric + '_known_sum'];
  const exact = group[metric];
  if (total > 0 && known === total && exact !== null && exact !== undefined) return String(exact);
  if (Number.isInteger(known) && known > 0 && sum !== null && sum !== undefined) return `${sum} • ${known}/${total}`;
  return `нет данных • 0/${total}`;
}
function date(input, full=false){if(!input)return '—';const d=new Date(typeof input==='number'?input*1000:input);return Number.isNaN(d.getTime())?String(input):new Intl.DateTimeFormat('ru-RU',{timeZone:'Asia/Yekaterinburg',...(full?{day:'2-digit',month:'2-digit'}:{}),hour:'2-digit',minute:'2-digit',second:full?'2-digit':undefined}).format(d);}
function showError(error){$('error').textContent=message(error);$('error').hidden=false;}
function showNotice(note){$('notice').textContent=note;$('notice').hidden=false;$('notice').className='';}
function showLogin(){csrf='';snapshot=null;globalThis.VKAnalytics?.clear();$('pos-frame').removeAttribute('src');selectTab('overview');$('app').hidden=true;$('login-screen').hidden=false;}
async function request(path, data) {const response=await fetch(path,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(csrf?{'X-CSRF-Token':csrf}:{})},...(data===undefined?{}:{body:JSON.stringify(data)})});let body;try{body=await response.json();}catch{throw new Error('backend_invalid_response');}if(!response.ok||body.error){if(response.status===401&&path!='/api/login')showLogin();throw new Error(body.error||'backend_error');}return body;}
async function rpc(operation,fields={}){return request('/api/rpc',{operation,...fields});}
// BEGIN account creation helper
async function createAccountAndLogin(name) {
  const created = await rpc('add_account', {name});
  if (!Number.isInteger(created.account) || created.account < 1) {
    throw new Error('backend_invalid_response');
  }
  try {
    await rpc('login_start', {account:created.account});
    return {account:created.account, loginStarted:true};
  } catch (error) {
    // The registry row already exists. Never issue add_account a second time
    // just because opening the VM browser failed or its outcome is unknown.
    return {account:created.account, loginStarted:false, error:error.message};
  }
}
// END account creation helper
function table(target, headings, rows){const wrap=$(target);wrap.replaceChildren();if(!rows.length){wrap.append(text('p','Нет данных','empty'));return;}const t=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const name of headings)hr.append(text('th',name));head.append(hr);t.append(head);const body=document.createElement('tbody');for(const row of rows){const tr=document.createElement('tr');for(const item of row){const td=document.createElement('td');if(item instanceof Node)td.append(item);else td.textContent=value(item);tr.append(td);}body.append(tr);}t.append(body);wrap.append(t);}
const states={verified:'Подтверждено',uncertain:'Неопределённый исход',already:'Уже выполнено',pending:'В очереди',running:'В работе',complete:'Завершено',batch_complete:'Пачка завершена',attention:'Требует проверки',counter_refresh_budget:'Бюджет чтения исчерпан',batch_spacing_wait:'Ожидание интервала',failed:'Не выполнено',skipped:'Пропущено',planned:'Запланировано'};
function status(input){return text('span',`${states[input]||input||'—'}${input==='verified'?' · verified':input==='uncertain'?' · uncertain':''}`,'result '+(input||''));}
function source(post){if(!post.group_id||!post.post_id)return text('span','—');const id=`${Math.abs(post.group_id)}_${post.post_id}`,link=text('a',id);link.href=`https://vk.com/wall-${id}`;link.target='_blank';link.rel='noreferrer noopener';return link;}
function flexibleAccountFlow(){return snapshot?.account_flow === 'add_any';}
function accountName(id){const account=(snapshot?.accounts||[]).find(a=>a.account===id);return account?(flexibleAccountFlow()?account.name||'Новый аккаунт':`${account.name} · №${id}`):`№${id}`;}
function actionRetrySummary(retry) {
  if (!retry) return '';
  return ` · Один повтор: ${retry.enabled ? 'включён' : 'выключен'} · Неудач подряд: ${value(retry.consecutive_failures)}/3 · Удержано: ${value(retry.held)} · Ожидает повтора: ${value(retry.pending)}`;
}
function jobAttemptHistory(job) {
  const recovery = job.recovery;
  if (!recovery) return `${job.reason||'—'}${job.attempt_id?` · попытка ${job.attempt_id}`:''}`;
  const cell = text('div', '');
  const labels = {running:'Проверка / выполнение', pending:'Ожидает повтора', held:'Удержано: исход не подтверждён', complete:'Проверка завершена', interrupted:'Прервано: требуется проверка'};
  cell.append(text('div', labels[recovery.state] || recovery.state || 'Состояние неизвестно'));
  for (const attempt of recovery.attempts || []) {
    const row = text('div', `${attempt.pass === 1 ? 'Первая попытка' : 'Повтор'} · ${attempt.attempt_id == null ? 'без клика' : `попытка ${attempt.attempt_id}`} · `);
    row.append(status(attempt.outcome), text('span', ` · ${attempt.reason || '—'}`));
    cell.append(row);
  }
  return cell;
}
function renderJobs(){const selected=$('job-account').value;const jobs=(snapshot?.jobs||[]).filter(j=>!selected||String(j.account)===selected);table('jobs-table',['Время YEKT','Аккаунт','Действие','Источник','Назначение','Результат','Причина / попытки'],jobs.map(j=>[date(typeof j.slot==='string'?j.slot:j.created_at,true),accountName(j.account),{like:'Лайк',personal:'Личный репост',personal_repost:'Личный репост',community:'В сообщество',community_repost:'В сообщество'}[j.kind]||j.kind,source(j),j.community_id||'Личная страница',status(j.status),jobAttemptHistory(j)]));}
// BEGIN timeline helpers
function timelineState(slot, batch, now, heartbeat={}, wallNow=now) {
  if (!batch) {
    return Number.isFinite(now) && Date.parse(slot) + 120000 < now
      ? {css:'skipped', label:'Пропущено: время запуска истекло'}
      : {css:'pending', label:'Запланировано'};
  }
  const code=batch.status;
  if (code==='complete' || code==='batch_complete' || code==='manual_four_complete')
    return {css:'verified', label:'Пачка завершена'};
  if (code==='attention')return {css:'uncertain', label:'Требует проверки'};
  if (!batch.finished_at && (code==='running' || code==='batch_spacing_wait')) {
    const at=Date.parse(heartbeat.at),age=wallNow-at;
    const current=Number.isFinite(age) && age>=-5000 && age<=180000 &&
      Number.isInteger(heartbeat.active_slot) && heartbeat.active_slot>=0 &&
      heartbeat.active_slot===batch.slot && ['refreshing_counters','batch_spacing_wait','executing_actions'].includes(heartbeat.phase);
    const waiting = heartbeat.phase==='batch_spacing_wait';
    const detail = waiting && typeof workerHeartbeat==='function' ? workerHeartbeat(heartbeat,wallNow).phaseText : '';
    return current?{css:'running', label:waiting?(detail || 'Ожидание интервала 9 мин'):'В работе'}
      :{css:'stale',label:'Нет свежего сигнала worker; пачка не завершена'};
  }
  const skips={batch_spacing_wait:'интервал9мин между пачками',
    batch_spacing_deadline:'интервал 9 мин не помещается до границы следующей пачки',
    counter_refresh_budget:'бюджет чтения исчерпан',counter_refresh_empty:'нет свежих подходящих постов',
    counter_refresh_stopped:'подготовка остановлена',counter_refresh_deadline:'оставлено время для следующей пачки',waiting_profile_lock:'профиль был занят',skipped:'без выполнения'};
  if (batch.finished_at && skips[code])return {css:'skipped',label:`Пропущено: ${skips[code]}`};
  const early={batch_window_or_budget_closed:'старый код не различал причину',
    batch_action_deadline:'достигнута граница действий за минуту до следующей пачки',
    batch_work_window_closed:'закрыто настроенное рабочее окно',
    batch_daily_quota_reached:'достигнута дневная квота действий',
    batch_stop_requested:'запрошена остановка worker',batch_account_disabled:'аккаунт отключён до действия',
    batch_day_changed:'наступил другой календарный день YEKT',
    batch_guard_rejected:'действие отклонено защитной проверкой; причина не уточнена',
    queue_empty:'нет подходящих действий',
    reserve_counter_refresh_budget:'исчерпан бюджет обновления резерва',reserve_counter_refresh_empty:'нет свежих подходящих записей в резерве',
    reserve_counter_refresh_stopped:'обновление резерва остановлено',reserve_waiting_profile_lock:'профиль занят при обновлении резерва',
    reserve_counter_refresh_deadline:'оставлено время для следующей пачки',
    technical_error_deferred:'техническая ошибка до отправки'};
  if (batch.finished_at && early[code])return {css:'partial',label:`Завершено досрочно: ${early[code]}; результаты — в заданиях`};
  return {css:'uncertain',label:`Требует проверки: ${code||'неизвестный результат'}`};
}
// END timeline helpers
function renderTimeline(){
  const timeline=$('timeline');timeline.replaceChildren();
  const schedule=snapshot?.scheduler||{},slots=schedule.slots||[],batches=schedule.batches||[];
  if(!slots.length&&!batches.length){timeline.append(text('p','Сохранённых интервалов пока нет','muted'));return;}
  const all=[...new Set([...slots,...batches.filter(b=>typeof b.slot==='string').map(b=>b.slot)])].sort();
  const grouped=new Map();
  for(const slot of all){const hour=date(slot).slice(0,2)+':00';if(!grouped.has(hour))grouped.set(hour,[]);grouped.get(hour).push(slot);}
  for(const [hour,items]of grouped){
    const group=text('div','','timeline-hour');group.append(text('strong',hour));const marks=text('div','','slots');
    for(const slot of items){
      const batch=batches.find(b=>(typeof b.slot==='number'?slots[b.slot]:b.slot)===slot);
      const state=timelineState(slot,batch,Date.parse(snapshot?.now),schedule.heartbeat||{},Date.now());
      const mark=text('span','','slot '+state.css);
      mark.title=`${date(slot,true)} · ${state.label}${batch?.started_at?` · начало ${date(batch.started_at)}`:''}`;
      mark.setAttribute('aria-label',mark.title);marks.append(mark);
    }
    group.append(marks);timeline.append(group);
  }
}
// BEGIN timing helpers
function timingSeconds(n){return typeof n==='number'&&Number.isFinite(n)?`${Number(n.toFixed(1))} с`:'—';}
function timingReason(row){
  const names={late_claim:'поздний захват слота',spacing_wait:'ожидание интервала 540 с',
    counter_refresh_deadline:'истёк срок чтения счётчиков',action_deadline:'истёк срок нового intent',
    no_candidate:'нет подходящего кандидата',platform_block:'защита VK',guard_block:'защитная остановка',
    quota:'дневная квота',stop_requested:'запрошена остановка',day_changed:'сменился день YEKT'};
  const reasons=(row.causes||[]).map(c=>names[c]).filter(Boolean);
  const next=row.next_not_started_before_intent===true&&Number.isInteger(row.attempts)&&
    Number.isInteger(row.batch_capacity)&&row.attempts>0&&row.attempts<row.batch_capacity&&row.batch_capacity<=6
    ? row.attempts+1 : row.next_not_started_before_intent===undefined&&row.fourth_not_started_before_intent?4:null;
  const ordinal={2:'вторая',3:'третья',4:'четвёртая',5:'пятая',6:'шестая'};
  if(ordinal[next])reasons.push(`${ordinal[next]} операция не запускалась, без intent и попытки`);
  if(row.unclaimed_after_grace)reasons.push('слот не захвачен до S + 120 с; пропуск без догоняющего запуска');
  if(!row.telemetry_available)reasons.push('этапы этой пачки не измерялись');
  return reasons.join('; ')||'—';
}
// END timing helpers
function renderTiming(){
  const report=snapshot?.scheduler?.timing,summary=report?.summary||{};
  if(!$('timing-summary'))return;
  $('timing-summary').textContent=report?.slots?.length?
    `Теоретическая вместимость, мест для попыток: ${value(summary.theoretical_attempts)}. Дневная квота: ${value(summary.daily_quota)} попыток. Зарегистрировано: ${value(summary.attempts)}; verified: ${value(summary.verified)}. Пустых завершённых пачек: ${value(summary.empty)}; неполных: ${value(summary.partial)}; незахваченных слотов после срока: ${value(summary.missed)}.`:
    report?.unavailable?'Диагностика времени недоступна; основные результаты показаны отдельно.':'Сохранённого плана для отчёта пока нет.';
  $('timing-coverage').textContent='Вместимость не гарантирует подтверждения. Прочерк — нет измерения. Исторические попытки без телеметрии показаны по заданиям; переносимые повторы могут ограничивать точность привязки к слоту.'+(report?.diagnostics_truncated?' Выборка измерений ограничена.':'');
  table('timing-slots',['Слот и границы YEKT','Фактические отметки YEKT','Сроки YEKT','Окно intent','Попытки / мест · verified','Причина'],(report?.slots||[]).map(r=>{
    const marks=text('div','','timing-marks');
    for(const [label,key]of [['Захват','claimed_at'],['Счётчики','refresh_finished_at'],['Интервал готов','spacing_ready_at'],['Первый intent','first_action_at']])marks.append(text('div',`${label}: ${date(r[key],true)}`));
    const bounds=text('div','','timing-marks'),limits=text('div','','timing-marks');
    for(const [label,key]of [['Слот','S'],['Следующий','N'],['Конец дня','E']])bounds.append(text('div',`${label}: ${date(r[key],true)}`));
    for(const [label,key]of [['Чтение','Dread'],['Новый intent','Dintent'],['Клик','click_deadline']])limits.append(text('div',`${label}: ${date(r[key],true)}`));
    return [bounds,marks,limits,timingSeconds(r.intent_seconds_available),`${value(r.attempts)} / ${value(r.batch_capacity)} · ${value(r.verified)}`,timingReason(r)];
  }));
  const names={browser_open:'Открытие браузера',source_navigation:'Переход к источнику',source_guard:'Проверка источника и аккаунта',community_prepare:'Подготовка сообщества',click:'Финальный клик',confirmation:'Подтверждение',cooldown:'Обязательная пауза'};
  table('timing-stages',['Этап','Полных / прерванных измерений','Медиана','p95'],Object.entries(report?.stages||{}).map(([key,r])=>[names[key]||key,`${value(r.samples)} / ${value(r.failed_samples)}`,timingSeconds(r.median_ms===null?null:r.median_ms/1000),timingSeconds(r.p95_ms===null?null:r.p95_ms/1000)]));
}
function action(label,run,cls='outline'){const b=text('button',label,cls);b.type='button';b.onclick=async()=>{if(busy)return;b.disabled=true;busy=true;$('error').hidden=true;try{await run();}catch(e){showError(e.message);}finally{b.disabled=false;busy=false;}};return b;}
function reportAccountCheck(accountId) {
  const account = (snapshot?.accounts || []).find(row => row.account === accountId);
  const auth = accountAuthentication(account);
  showNotice(flexibleAccountFlow()
    ? `${accountName(accountId)}: ${auth.label}. VK ID: ${value(auth.observed ?? account?.vk_id)}.`
    : `Проверка аккаунта №${accountId}: ${auth.label}. Ожидался VK ID: ${value(auth.expected)}; обнаружен VK ID: ${value(auth.observed)}.`);
  $('notice').className = auth.canEnable ? '' : 'notice-warning';
}
const activeLoginStates = new Set(['opening', 'waiting_browser', 'awaiting_login', 'ready', 'wrong_account']);
const watchedLoginChecks = new Map();
let loginPollBusy = false;
function applyLoginStatus(accountId, result) {
  if (!result || typeof result.state !== 'string') throw new Error('backend_invalid_response');
  const account = (snapshot?.accounts || []).find(row => row.account === accountId);
  if (!account) return null;
  account.login = result;
  account.authentication = result.authentication && typeof result.authentication === 'object'
    ? result.authentication
    : {state:'unverified', expected_vk_id:account.vk_id ?? null, observed_vk_id:null,
      checked_at:null, fresh:false, can_finish:false, reason:'awaiting_identity_evidence'};
  return account;
}
function reportWatchedLogin(account) {
  if (!watchedLoginChecks.has(account.account)) return false;
  const phase = account.login?.state;
  const proof = account.authentication;
  if (['checked', 'ready', 'wrong_account', 'complete'].includes(phase) && proof?.fresh === true) {
    const signature = JSON.stringify([proof.state, proof.expected_vk_id, proof.observed_vk_id]);
    if (watchedLoginChecks.get(account.account) !== signature) reportAccountCheck(account.account);
    watchedLoginChecks.set(account.account, signature);
    if (!activeLoginStates.has(phase)) watchedLoginChecks.delete(account.account);
    return true;
  }
  if (!activeLoginStates.has(phase)) {
    const labels = {unverified:'Вход не проверен', stale:'Нет свежего сигнала проверки', closed:'Окно закрыто', expired:'Время проверки истекло', error:'Ошибка проверки', idle:'Проверка не выполняется'};
    showNotice(`Проверка аккаунта №${account.account}: ${labels[phase] || 'результат не подтверждён'}. Текущий вход не подтверждён этой проверкой.`);
    $('notice').className = 'notice-warning';
    watchedLoginChecks.delete(account.account);
    return true;
  }
  return false;
}
async function requestAccountCheck(accountId) {
  const result = await rpc('login_check', {account:accountId});
  const account = applyLoginStatus(accountId, result);
  if (!account) return;
  watchedLoginChecks.set(accountId, null);
  if (!reportWatchedLogin(account)) showNotice(`Проверка запущена для аккаунта №${accountId}. Ожидаю сравнение VK ID; повторно открывать браузер не требуется.`);
  renderAccounts();
}
async function pollLoginStatuses() {
  if (!csrf || !snapshot || busy || loginPollBusy || $('account-dialog').open) return;
  const accounts = (snapshot.accounts || []).filter(account => activeLoginStates.has(account.login?.state));
  if (!accounts.length) return;
  loginPollBusy = true;
  try {
    let changed = false;
    for (const current of accounts) {
      if (busy || $('account-dialog').open || !csrf) break;
      const result = await rpc('login_status', {account:current.account});
      if (busy || $('account-dialog').open || !csrf) break;
      const account = applyLoginStatus(current.account, result);
      if (flexibleAccountFlow() && result.state === 'complete' && Number.isSafeInteger(result.canonical_account) && result.canonical_account > 0) {
        watchedLoginChecks.delete(current.account);
        // Connecting an existing VK identity can replace the temporary row.
        // Reload the canonical visible rows once; never create or start again.
        await refresh();
        showNotice('Аккаунт подключён. Профиль сохранён. Автоматика включается отдельно в настройках.');
        changed = false;
        break;
      }
      if (account) { reportWatchedLogin(account); changed = true; }
    }
    if (changed) renderAccounts();
  } catch (error) {
    showError(error.message);
  } finally {
    loginPollBusy = false;
  }
}
function renderAccounts() {
  const list = $('account-list');
  const flexible = flexibleAccountFlow();
  $('account-flow-hint').textContent = flexible
    ? 'Добавьте аккаунт, войдите в окне VM; профиль сохранится автоматически. Автоматика включается отдельно в настройках.'
    : 'Отключение действует сразу. Квоты и назначения — с первого ещё не запланированного дня.';
  list.replaceChildren();
  for (const account of snapshot?.accounts || []) {
    const auth = accountAuthentication(account);
    const row = text('article', '', 'account-row');
    const heading = text('div', '', 'account-title');
    heading.append(text('h2', accountName(account.account)), text('span', account.enabled ? 'Автоматика включена' : 'Автоматика отключена', 'muted'));
    row.append(heading);
    if (flexible) {
      if (account.vk_id) row.append(text('p', `VK ID: ${account.vk_id}`, 'muted footnote'));
      const phases = {opening:'Открывается окно входа на VM', waiting_browser:'Ожидается завершение пачки worker. Окно входа откроется автоматически на VM.', awaiting_login:'Войдите в VK в окне VM', complete:'Аккаунт подключён', error:'Не удалось подключить аккаунт'};
      if (phases[account.login?.state]) row.append(text('p', phases[account.login.state], 'muted'));
    } else row.append(text('p', account.bound ? `Сохранённая привязка: VK ID ${value(account.vk_id)}` : 'Сохранённой привязки VK ID нет', 'muted footnote'));
    row.append(text('p', auth.label, 'authentication-status ' + auth.state));
    const details = text('div', '', 'account-details');
    if (!flexible) details.append(text('span', `Ожидался VK ID: ${value(auth.expected)}`), text('span', `Обнаружен VK ID: ${value(auth.observed)}`));
    details.append(text('span', `Проверено: ${date(auth.checkedAt, true)} YEKT`));
    row.append(details);
    if (!auth.canEnable) row.append(text('p', flexible ? 'Для включения автоматики завершите подключение и получите свежую проверку входа.' : 'Включение недоступно без свежего подтверждения нужного аккаунта. Сохранённая привязка и старый статус ready не подтверждают текущий вход.', 'muted'));
    if (account.login?.reason === 'legacy_identity_not_compared') row.append(text('p', 'Открытая сессия входа создана прежней версией и не сравнивает VK ID. Дождитесь её штатного завершения, затем выполните «Проверить вход». Активное окно автоматически не закрывается.', 'muted footnote'));
    const policy = account.effective_policy || account.policy || {}, usage = account.usage || {}, remaining = account.remaining || {};
    row.append(text('p', `Лайки ${value(usage.like)} / ${value(policy.likes)}, осталось ${value(remaining.like)} · Личные ${value(usage.personal)} / ${value(policy.personal_reposts)}, осталось ${value(remaining.personal)} · Сообщества ${value(usage.community)} / ${value(policy.community_reposts)}, осталось ${value(remaining.community)}. Назначения: ${(policy.communities || []).join(', ') || 'не заданы'}`, 'account-policy'));
    if (account.effective_policy && JSON.stringify(account.effective_policy) !== JSON.stringify(account.policy)) {
      const future = account.policy || {};
      row.append(text('p', `Будущие квоты: лайки ${value(future.likes)}, личные ${value(future.personal_reposts)}, сообщества ${value(future.community_reposts)} · назначения ${(future.communities || []).join(', ') || 'не заданы'}`, 'muted'));
    }
    const actions = text('div', '', 'account-actions');
    actions.append(
      action('Настройки', () => editAccount(account)),
      action('Начать вход', async () => {
        await rpc('login_start', {account:account.account});
        await refresh();
        showNotice(flexible ? 'Вход запущен на Windows VM. Войдите в окне VM; профиль сохранится автоматически.' : `Окно входа аккаунта №${account.account} открыто на Windows VM. Введите пароль и код VK только там, затем проверьте вход.`);
      }),
      action('Проверить вход', () => requestAccountCheck(account.account))
    );
    if (flexible && activeLoginStates.has(account.login?.state)) {
      actions.children[1].disabled = true;
      actions.children[2].disabled = true;
    }
    if (!flexible) {
      actions.append(action('Завершить вход', async () => { await rpc('login_finish', {account:account.account}); await refresh(); reportAccountCheck(account.account); }));
      actions.children[actions.children.length - 1].disabled = !auth.canFinish;
    }
    row.append(actions);
    row.append(text('p', flexible ? 'Добавьте аккаунт, войдите в окне VM; профиль сохранится автоматически. Пароль и код VK вводите только в окне VM. Перед новым входом отключите автоматику в настройках.' : 'Перед входом отключите аккаунт в настройках. Подключитесь к VM, нажмите «Начать вход», введите пароль и код VK только в окне VM, затем «Проверить вход» и «Завершить вход». Проверка не включает автоматику.', 'muted footnote'));
    list.append(row);
  }
  if (!list.children.length) list.append(text('p', 'Аккаунтов пока нет. Добавьте отдельный профиль.', 'empty'));
}
function rangeLabel(from, until) {
  if (!from && !until) return 'нет данных';
  return `${date(from, true)} — ${date(until, true)}`;
}
function fillGroupFilters() {
  for (const id of ['post-group', 'statistics-group']) {
    const field = $(id), selected = field.value;
    field.replaceChildren(new Option('Все группы', ''));
    for (const group of snapshot?.groups || []) field.add(new Option(`${group.name} · ${group.id}`, group.id));
    field.value = selected;
  }
}
function renderGroups() {
  table('groups-table', ['Группа', 'ID', 'Постов в выборке', 'Период публикаций YEKT', 'Лайки', 'Комментарии', 'Репосты', 'Просмотры', 'Наблюдения YEKT', 'Записи'],
    (snapshot?.groups || []).map(group => [
      group.name, group.id, group.posts,
      rangeLabel(group.published_from || group.latest_published_from, group.published_until || group.latest_published_until),
      cachedMetric(group, 'like_count'), cachedMetric(group, 'comment_count'),
      cachedMetric(group, 'repost_count'), cachedMetric(group, 'view_count'),
      rangeLabel(group.observed_at, group.observed_until),
      action('Публикации', async () => { $('post-group').value = String(group.id); selectTab('posts'); await loadPosts(); })
    ]));
  const progress = snapshot?.statistics_refresh || {};
  const labels = {queued:'В очереди', running:'Обновление', waiting_lock:'Ожидание профиля / блокировки', complete:'Обновление завершено', partial:'Частичное обновление', error:'Ошибка обновления', budget:'Бюджет API исчерпан'};
  const counts = [
    progress.failures !== null && progress.failures !== undefined ? `ошибок: ${progress.failures}` : '',
    progress.rejected_posts !== null && progress.rejected_posts !== undefined ? `отклонено публикаций: ${progress.rejected_posts}` : ''
  ].filter(Boolean).map(label => ` · ${label}`).join('');
  $('statistics-progress').textContent = progress.state
    ? `${labels[progress.state] || 'Неизвестное состояние'} · группы ${value(progress.groups_done)}/${value(progress.total)} · запросов ${value(progress.requests)}${counts} · ${date(progress.at, true)} YEKT${progress.reason ? ` · причина: ${progress.reason}` : ''}`
    : 'Обновление статистики ещё не запрашивалось.';
  $('refresh-statistics').disabled = ['queued','running','waiting_lock'].includes(progress.state);
}
let loadedGroupPosts = {id:null, posts:[]};
async function loadPosts() {
  const id = $('post-group').value;
  const fields = id ? {group_id:Number(id), limit:100} : {limit:100};
  const result = await rpc('posts', fields);
  if ($('post-group').value !== id) return;
  loadedGroupPosts = {id, posts:result.posts || []};
  renderPosts(loadedGroupPosts.posts);
}
function selectTab(id) {
  activeTab = id;
  for (const panel of document.querySelectorAll('.tab-panel')) panel.hidden = panel.id !== id;
  for (const button of document.querySelectorAll('[data-tab]')) button.classList.toggle('active', button.dataset.tab === id);
  if (id === 'pos' && csrf && !$('pos-frame').hasAttribute('src')) $('pos-frame').src = '/pos/';
  globalThis.VKAnalytics?.setActive(id === 'analytics', csrf);
}
globalThis.addEventListener?.('analytics-login-required',()=>showLogin());
globalThis.addEventListener?.('analytics-ready',()=>globalThis.VKAnalytics?.setActive(activeTab==='analytics',csrf));

globalThis.addEventListener?.('message', event => {
  if (event.origin === location.origin && event.source === $('pos-frame').contentWindow && event.data?.type === 'pos-login-required') showLogin();
});

function renderPosts(posts){const selected=$('post-group').value;const group=(snapshot?.groups||[]).find(row=>String(row.id)===selected);$('posts-heading').textContent=selected?`До 100 последних публикаций: ${group?.name||selected}`:'Последние 100 публикаций каталога';$('posts-caption').textContent=selected?'Последние публикации выбранной группы из кеша API. Прочерк означает неизвестное значение.':'Общая последняя выборка каталога. Выберите группу для её 100 последних публикаций. Прочерк означает неизвестное значение.';table('posts-table',['Публикация YEKT','Группа','Источник','Лайки','Комментарии','Репосты','Просмотры','Наблюдение YEKT'],posts.slice(0,100).map(p=>[date(p.published_at,true),p.group_name||p.group_id,source(p),p.like_count,p.comment_count,p.repost_count,p.view_count,date(p.observed_at,true)]));}
function renderWorkerState(){if(!snapshot)return;const s=snapshot.scheduler||{};const heartbeat=workerHeartbeat(s.heartbeat);$('heartbeat').textContent=`Последний сигнал: ${date(heartbeat.at,true)} YEKT · ${heartbeat.freshnessText} · данные backend: ${date(snapshot.now,true)}`;$('worker-state').textContent=heartbeat.stateText;$('worker-state').className='muted worker-state '+heartbeat.state;$('worker-phase').textContent=`Этап: ${heartbeat.phaseText}`;$('worker-mode').textContent=`Режим: ${heartbeat.modeText}`;}
function render(){const s=snapshot.scheduler||{};const windowNode=$('work-window');if(windowNode)windowNode.textContent=scheduleWindowText(s);const batchNode=$('batch-policy');if(batchNode)batchNode.textContent=scheduleBatchText(s);$('execution-host').textContent=`Исполнение: ${snapshot.execution_host||'Windows VM'}`;renderWorkerState();$('safety-state').textContent=`${s.paused?`Пауза: ${typeof s.paused==='string'?s.paused:'защитная остановка'}. `:''}Активных uncertain: ${value(s.active_uncertain)} · Изолировано постов: ${value(s.isolated_posts)}${actionRetrySummary(s.action_retry)}`;$('policy-day').textContent=`Лимиты — ${s.policy_effective_day||'с первого не запланированного дня'}`;const selected=$('job-account').value;$('job-account').replaceChildren(new Option('Все',''));for(const a of snapshot.accounts||[])$('job-account').add(new Option(`${a.name} · №${a.account}`,a.account));$('job-account').value=selected;fillGroupFilters();renderJobs();renderTimeline();renderTiming();renderAccounts();if(!$('post-group').value)renderPosts(snapshot.posts||[]);else if(loadedGroupPosts.id===$('post-group').value)renderPosts(loadedGroupPosts.posts);else renderPosts([]);renderGroups();$('updated').textContent=`Обновлено ${new Date().toLocaleTimeString('ru-RU')}`;}
async function refresh(){const result=await rpc('snapshot');snapshot=result;$('error').hidden=true;render();}
function editAccount(account){
  const flexible=flexibleAccountFlow();
  $('account-id').value=account?.account||'';
  $('dialog-title').textContent=account?'Настройки аккаунта':'Добавить аккаунт';
  $('name-label').hidden=!!account&&!flexible;
  $('policy-fields').hidden=!account;
  $('account-name').required=!account&&!flexible;
  for(const id of ['likes','personal-reposts','community-reposts'])$(id).required=!!account;
  $('account-name').value=account?.name||'';
  $('account-name').placeholder=flexible?'Новый аккаунт':'';
  $('account-enabled').checked=!!account?.enabled;
  const authentication=accountAuthentication(account);
  $('account-enabled').disabled=!account?.enabled&&!authentication.canEnable;
  $('account-auth-hint').textContent=authentication.canEnable?'Свежая проверка входа получена. Включение остаётся отдельным действием.':flexible?'Сначала подключите аккаунт и проверьте вход. Отключение доступно сразу.':'Включение недоступно: сначала подтвердите вход именно в нужный VK ID. Отключение доступно сразу.';
  $('likes').value=account?.policy?.likes??(flexible?0:50);
  $('personal-reposts').value=account?.policy?.personal_reposts??(flexible?0:10);
  $('community-reposts').value=account?.policy?.community_reposts??(flexible?0:20);
  $('communities').value=(account?.policy?.communities||[]).join(', ');
  $('form-error').textContent='';
  $('account-dialog').showModal();
}
$('login-form').onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;$('login-error').textContent='';try{const result=await request('/api/login',{username:$('username').value,password:$('password').value});csrf=result.csrf;$('password').value='';$('login-screen').hidden=true;$('app').hidden=false;if(location.hash==='#analytics')selectTab('analytics');await refresh();}catch(error){if(csrf)showError(error.message);else $('login-error').textContent=message(error.message);}finally{button.disabled=false;}};
$('logout').onclick=async()=>{try{await request('/api/logout',{});showLogin();}catch(e){showError(e.message);}};
$('refresh').onclick=async()=>{const b=$('refresh');b.disabled=true;try{await refresh();}catch(e){showError(e.message);}finally{b.disabled=false;}};
$('refresh-posts').onclick=async()=>{const b=$('refresh-posts');b.disabled=true;try{await loadPosts();}catch(e){showError(e.message);}finally{b.disabled=false;}};
$('post-group').onchange=async()=>{try{await loadPosts();}catch(error){showError(error.message);}};
$('refresh-statistics').onclick=async()=>{const button=$('refresh-statistics');button.disabled=true;try{const selected=$('statistics-group').value;await rpc('refresh_statistics',selected?{group_id:Number(selected)}:{});await refresh();showNotice('Читающее обновление статистики запрошено. Ход выполнения показан ниже; действия VK не отправляются.');}catch(error){button.disabled=false;showError(error.message);}};
$('job-account').onchange=renderJobs;
$('add-account').onclick=()=>editAccount(null);$('close-dialog').onclick=()=>$('account-dialog').close();
$('account-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $('form-error').textContent = '';
  let saved = false, note = '', loginError = null;
  try {
    const account = Number($('account-id').value);
    if (account) {
      const current=(snapshot?.accounts||[]).find(row=>row.account===account);
      if($('account-enabled').checked&&!accountAuthentication(current).canEnable)throw new Error('Включение недоступно без свежего подтверждения нужного аккаунта.');
      const raw = $('communities').value.trim();
      const groups = raw ? raw.split(',').map(group => Number(group.trim())) : [];
      if (groups.length > 2 || groups.some(group => !Number.isSafeInteger(group) || group <= 0) || new Set(groups).size !== groups.length) {
        throw new Error('Укажите до двух различных положительных ID сообществ.');
      }
      const communityQuota=Number($('community-reposts').value);
      if (communityQuota>0 && groups.length!==2) throw new Error('community_destinations_required');
      if (communityQuota%2!==0) throw new Error('Квота репостов в сообщества должна быть чётной: поровну между двумя группами.');
      await rpc('configure_account', {
        account, enabled:$('account-enabled').checked,
        ...(flexibleAccountFlow()?{name:$('account-name').value.trim()||'Новый аккаунт'}:{}),
        likes:Number($('likes').value),
        personal_reposts:Number($('personal-reposts').value),
        community_reposts:Number($('community-reposts').value), communities:groups
      });
      note = 'Настройки сохранены. Новые квоты и назначения — с первого ещё не запланированного дня, не раньше завтра; отключение — сразу.';
    } else {
      const created = await createAccountAndLogin($('account-name').value.trim()||(flexibleAccountFlow()?'Новый аккаунт':''));
      note = flexibleAccountFlow() ? (created.loginStarted
        ? 'Аккаунт добавлен. Вход запущен на Windows VM. Войдите в окне VM; профиль сохранится автоматически.'
        : 'Аккаунт добавлен. Окно входа не подтверждено. После проверки причины используйте «Начать вход» у этого аккаунта.') : created.loginStarted
        ? `Аккаунт №${created.account} создан и сохранён. Окно входа в новый профиль открыто на Windows VM. Подключитесь к VM, затем проверьте и завершите вход.`
        : `Аккаунт №${created.account} создан и сохранён. Окно входа не подтверждено. После проверки причины используйте «Начать вход» у этого аккаунта.`;
      loginError = created.error || null;
    }
    saved = true;
    $('account-dialog').close();
    await refresh();
    showNotice(note);
    if (loginError) showError(loginError);
  } catch (error) {
    if (saved) {
      showNotice(note);
      showError(error.message);
    } else {
      $('form-error').textContent = errors[error.message] ? message(error.message) : error.message;
    }
  } finally {
    button.disabled = false;
  }
};
for(const button of document.querySelectorAll('[data-tab]'))button.onclick=async()=>{selectTab(button.dataset.tab);if(activeTab==='posts'){try{await loadPosts();}catch(error){showError(error.message);}}};
(async()=>{try{const session=await request('/api/session');if(!session.authenticated){showLogin();return;}csrf=session.csrf;$('app').hidden=false;if(location.hash==='#analytics')selectTab('analytics');try{await refresh();}catch(e){showError(e.message);}}catch{showLogin();}})();
setInterval(async()=>{if(csrf&&!busy&&!$('account-dialog').open){try{await refresh();}catch(e){showError(e.message);}}},60000);

setInterval(pollLoginStatuses, 3000);

// Refresh only time-sensitive cached status; never reset forms or fetch data.
setInterval(()=>{if(csrf&&snapshot){renderWorkerState();renderTimeline();}},30000);
