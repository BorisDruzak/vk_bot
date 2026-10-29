"""Explicit community destinations with an account-local, destination-keyed ledger.

Personal-wall reactions and the legacy command runner remain independent.
Dry-run only prepares a chooser: no likes, submissions, attempts or run records.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PlaywrightError

import vk_accounts as accounts
import vk_account_smoke as smoke
import vk_api_read as api
import vk_batch as engine
import vk_groups as multi
import vk_community_ui as ui


def init_state(db, vk_id: int) -> None:
    """Additive schema, bound to one actor; never adopt somebody else's ledger."""
    if type(vk_id) is not int or not 0<vk_id<=2**53-1:
        raise ValueError('Не задан точный числовой VK ID аккаунта')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS community_account_identity(
        id INTEGER PRIMARY KEY CHECK(id=1), vk_id INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS community_reposts(
        community_id INTEGER NOT NULL, group_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','already','verified','uncertain')),
        target_post_id INTEGER, updated_at TEXT NOT NULL,
        PRIMARY KEY(community_id,group_id,post_id));
      CREATE TABLE IF NOT EXISTS community_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, community_id INTEGER NOT NULL,
        requested_like INTEGER NOT NULL, requested_repost INTEGER NOT NULL,
        max_posts INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        result TEXT NOT NULL DEFAULT 'partial');
      CREATE TABLE IF NOT EXISTS community_run_posts(
        run_id INTEGER NOT NULL,group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
        PRIMARY KEY(run_id,group_id,post_id));
      CREATE TABLE IF NOT EXISTS community_run_attempts(
        run_id INTEGER NOT NULL,attempt_id INTEGER NOT NULL UNIQUE,
        PRIMARY KEY(run_id,attempt_id));
      CREATE TABLE IF NOT EXISTS community_source_refs(
        group_id INTEGER NOT NULL,post_id INTEGER NOT NULL,
        canonical_group_id INTEGER NOT NULL,canonical_post_id INTEGER NOT NULL,
        PRIMARY KEY(group_id,post_id));
    ''')
    row=db.execute('SELECT vk_id FROM community_account_identity WHERE id=1').fetchone()
    if row and row['vk_id']!=vk_id:
        raise engine.StopRun('История репостов в сообщества принадлежит другому VK ID')
    if not row:
        if any(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
               for table in ('community_reposts','community_runs','community_run_attempts')):
            raise engine.StopRun('История сообществ без привязки аккаунта; автоматическое принятие запрещено')
        db.execute('INSERT INTO community_account_identity VALUES(1,?)',(vk_id,))
    db.commit()


def get_state(db, community_id: int, group_id: int, post_id: int) -> dict:
    row=db.execute('SELECT * FROM community_reposts WHERE community_id=? AND group_id=? AND post_id=?',
                   (community_id,group_id,post_id)).fetchone()
    return dict(row) if row else {'status':'pending','target_post_id':None}


def remember_reference(db,gid,pid,canonical_gid,canonical_pid):
    ui._ids(gid,pid)
    ui._canonical_ids(canonical_gid,canonical_pid)
    old=db.execute('SELECT canonical_group_id,canonical_post_id FROM community_source_refs WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
    if old and tuple(old)!=(canonical_gid,canonical_pid):
        raise engine.StopRun('Точный оригинал исходной публикации изменился; нужна проверка')
    db.execute('INSERT OR IGNORE INTO community_source_refs VALUES(?,?,?,?)',(gid,pid,canonical_gid,canonical_pid));db.commit()


def reference_state(db,community_id,gid,pid):
    ref=db.execute('SELECT canonical_group_id,canonical_post_id FROM community_source_refs WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
    cg,cp=tuple(ref) if ref else (gid,pid)
    rows=db.execute('''SELECT p.* FROM community_reposts p LEFT JOIN community_source_refs r
      ON r.group_id=p.group_id AND r.post_id=p.post_id
      WHERE p.community_id=? AND COALESCE(r.canonical_group_id,p.group_id)=?
      AND COALESCE(r.canonical_post_id,p.post_id)=? AND p.status!='pending'
      ORDER BY CASE WHEN p.status='uncertain' THEN 0 ELSE 1 END''',(community_id,cg,cp)).fetchall()
    return dict(rows[0]) if rows else {'status':'pending','target_post_id':None}


def require_reference_available(db,community_id,gid,pid):
    if db.execute('''SELECT 1 FROM community_reposts p LEFT JOIN community_source_refs r
        ON r.group_id=p.group_id AND r.post_id=p.post_id
        WHERE p.community_id=? AND p.status!='pending' AND r.group_id IS NULL LIMIT 1''',(community_id,)).fetchone():
        raise engine.StopRun('В назначении есть история без проверенного оригинала; нужна read-only сверка')
    if reference_state(db,community_id,gid,pid)['status']!='pending':
        raise engine.StopRun('Оригинал уже отправлен в это сообщество либо uncertain; повтор запрещён')


def write_state(db, community_id, group_id, post_id, status, *, target_post_id=None) -> None:
    if status not in ('pending','already','verified','uncertain'):
        raise ValueError('Неизвестный статус репоста в сообщество')
    if status in ('already','verified') and (type(target_post_id) is not int or target_post_id<=0):
        raise ValueError('Подтверждённому репосту нужен ID записи на целевой стене')
    if status=='pending' and get_state(db,community_id,group_id,post_id)['status']=='uncertain':
        raise engine.StopRun('Неопределённый репост нельзя автоматически вернуть в очередь')
    db.execute('''INSERT INTO community_reposts VALUES(?,?,?,?,?,?)
      ON CONFLICT(community_id,group_id,post_id) DO UPDATE SET status=excluded.status,
      target_post_id=excluded.target_post_id,updated_at=excluded.updated_at''',
      (community_id,group_id,post_id,status,target_post_id,engine.moment()))
    db.commit()


def select_candidates(db, groups, days, community_id, likes, reposts, *, source_db) -> list[dict]:
    """Read current public eligibility from source; account #1 reactions never cross accounts."""
    selected={g['id']:g for g in groups if g.get('enabled',True)}
    if not selected:return []
    path=Path(source_db)
    if not path.is_file():raise engine.StopRun('Общий каталог VK API отсутствует; сначала --discover')
    now=int(datetime.now(timezone.utc).timestamp())
    marks=','.join('?' for _ in selected)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory=sqlite3.Row
            public=source.execute(f'''SELECT a.group_id,a.post_id,a.published_at FROM api_seen_posts a
              JOIN post_publication d ON d.group_id=a.group_id AND d.post_id=a.post_id
              LEFT JOIN api_group_scan_progress sc ON sc.group_id=a.group_id
              WHERE a.group_id IN ({marks}) AND a.valid=1 AND d.exact=1
              AND a.published_at=d.published_at AND a.published_at BETWEEN ? AND ?
              AND (sc.status IS NULL OR sc.status!='error')
              ORDER BY a.published_at DESC,a.group_id,a.post_id DESC''',
              (*selected,now-days*86400,now+3600)).fetchall()
    except sqlite3.DatabaseError as exc:
        raise engine.StopRun('Не удалось прочитать точный публичный API-каталог') from exc
    multi.init_dates(db)
    result=[]
    for public_row in public:
        gid,pid=public_row['group_id'],public_row['post_id']
        engine.add_posts(db,gid,[pid])
        multi.save_date(db,gid,pid,public_row['published_at'],api.API_SOURCE,True)
        post=db.execute('SELECT like_status,repost_status FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
        state=get_state(db,community_id,gid,pid)
        if db.execute('SELECT 1 FROM post_access WHERE group_id=? AND post_id=?',(gid,pid)).fetchone():continue
        if state['status']=='uncertain' or post['like_status'] in ('uncertain','error'):continue
        want_like=likes>0 and selected[gid].get('like_enabled') and post['like_status']=='pending'
        want_repost=reposts>0 and selected[gid].get('repost_enabled') and state['status']=='pending'
        if want_like or want_repost:
            result.append({**dict(public_row),'like_status':post['like_status'],
                'repost_status':state['status'],'want_like':bool(want_like),'want_repost':bool(want_repost)})
    return result


def begin_run(db, community_id, likes, reposts, max_posts) -> int:
    if min(likes,reposts)<0 or reposts<1 or max_posts<1:raise ValueError('Неверные числа действий')
    cursor=db.execute('''INSERT INTO community_runs
       (community_id,requested_like,requested_repost,max_posts,started_at) VALUES(?,?,?,?,?)''',
       (community_id,likes,reposts,max_posts,engine.moment()))
    db.commit();return cursor.lastrowid


def progress(db, run_id) -> dict:
    result={'like_attempts':0,'repost_attempts':0,'like_verified':0,'repost_verified':0}
    for action in ('like','repost'):
        result[action+'_attempts']=db.execute('''SELECT COUNT(*) FROM community_run_attempts c
          JOIN action_attempts a ON a.id=c.attempt_id WHERE c.run_id=? AND a.action=?''',(run_id,action)).fetchone()[0]
    result['like_verified']=db.execute('''SELECT COUNT(*) FROM community_run_attempts c
      JOIN action_attempts a ON a.id=c.attempt_id JOIN posts p ON p.group_id=a.group_id AND p.post_id=a.post_id
      WHERE c.run_id=? AND a.action='like' AND p.like_status='verified' ''',(run_id,)).fetchone()[0]
    result['repost_verified']=db.execute('''SELECT COUNT(*) FROM community_run_attempts c
      JOIN community_runs r ON r.id=c.run_id JOIN action_attempts a ON a.id=c.attempt_id
      JOIN community_reposts p ON p.community_id=r.community_id AND p.group_id=a.group_id AND p.post_id=a.post_id
      WHERE c.run_id=? AND a.action='repost' AND p.status='verified' ''',(run_id,)).fetchone()[0]
    result['posts']=db.execute('SELECT COUNT(*) FROM community_run_posts WHERE run_id=?',(run_id,)).fetchone()[0]
    return result


class CommunityBudget:
    def __init__(self, db, run_id, community_id, likes, reposts):
        self.db,self.run_id,self.community_id=db,run_id,community_id
        self.requested={'like':likes,'repost':reposts};self.used=Counter()
        self.current_group_id=None;self.identity_guard=None
        saved=db.execute('SELECT community_id FROM community_runs WHERE id=?',(run_id,)).fetchone()
        if saved is None or saved['community_id']!=community_id:raise ValueError('Неверное назначение бюджета')
        p=progress(db,run_id)
        self.used.update({a:p[a+'_attempts'] for a in ('like','repost')})

    def available_actions(self, actions):
        engine.require_platform_unlocked(self.db)
        return {a for a in actions if a in self.requested and self.used[a]<self.requested[a]}

    def reserve(self, post_id, action):
        if self.identity_guard:self.identity_guard()
        if action not in self.available_actions({action}):raise engine.SafetyLimitReached('Бюджет запуска исчерпан')
        if self.current_group_id is None:raise ValueError('Не задан исходный владелец поста')
        gid=self.current_group_id
        with self.db:
            if action=='repost':
                self.db.execute('INSERT OR IGNORE INTO community_reposts VALUES(?,?,?,\'pending\',NULL,?)',
                                (self.community_id,gid,post_id,engine.moment()))
                updated=self.db.execute('''UPDATE community_reposts SET status='uncertain',updated_at=?
                  WHERE community_id=? AND group_id=? AND post_id=? AND status='pending' ''',
                  (engine.moment(),self.community_id,gid,post_id))
                if updated.rowcount!=1:raise engine.StopRun('Репост в это сообщество уже был или имеет uncertain')
            cursor=self.db.execute('INSERT INTO action_attempts(group_id,post_id,action,attempted_at) VALUES(?,?,?,?)',
                                   (gid,post_id,action,engine.moment()))
            self.db.execute('INSERT INTO community_run_attempts VALUES(?,?)',(self.run_id,cursor.lastrowid))
        self.used[action]+=1


def live_identity(context, expected_vk_id):
    if accounts.inspect_context(context,attempts=4)!=expected_vk_id:
        raise engine.StopRun('VK ID текущего профиля не совпадает с локальной привязкой')


def run_actions(db,cfg,groups,*,community_id,likes,reposts,max_posts,pause,profile,
                expected_vk_id,source_db,dry_run=False) -> dict:
    engine.require_platform_unlocked(db)
    rows=select_candidates(db,groups,cfg['lookback_days'],community_id,likes,reposts,source_db=source_db)
    if not rows:
        print('[COMMUNITY QUEUE] Нет подходящих публикаций для этого назначения')
        return {'prepared':0,'run_id':None}
    run_id=None if dry_run else begin_run(db,community_id,likes,reposts,max_posts)
    budget=None if dry_run else CommunityBudget(db,run_id,community_id,likes,reposts)
    prepared_count=0;result='partial'
    def source_ready(page,row,guard):
        engine.require_platform_unlocked(db)
        engine.go_to_post(page,row['group_id'],row['post_id']);guard()
        stamp=multi.exact_post_date_info(page,row['group_id'],row['post_id']).get('publishedAt')
        if (type(stamp) is not int or stamp!=row['published_at'] or
                not multi.date_allowed(stamp,cfg['lookback_days'],True)):
            raise engine.StopRun('Дата исходного поста отличается от VK API; действий не было')
    try:
        with sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(user_data_dir=str(profile),headless=False,
                         locale='ru-RU',viewport={'width':1320,'height':900})
            page=None
            try:
                page=context.new_page()
                guard=lambda:live_identity(context,expected_vk_id)
                engine.watch_rate_limit_responses(page);engine.configure_navigation_pacing(page,pause,db=db)
                if budget:budget.identity_guard=guard
                for row in rows[:max_posts]:
                    gid,pid=row['group_id'],row['post_id']
                    requested={a for a in ('like','repost') if row['want_'+a]}
                    allowed=requested if dry_run else budget.available_actions(requested)
                    if not allowed:continue
                    source_ready(page,row,guard)
                    if dry_run:
                        # No real actions at all, including requested likes. No run/attempt records.
                        if row['want_repost']:
                            proof=ui.prepare(page,gid,pid,community_id,identity_guard=guard)
                            prepared_count+=1
                            print(f'[DRY RUN] {gid}_{pid}: сообщество {community_id}, выбор подтверждён; отправки нет')
                            page.keyboard.press('Escape')
                        continue
                    budget.current_group_id=gid
                    db.execute('INSERT OR IGNORE INTO community_run_posts VALUES(?,?,?)',(run_id,gid,pid));db.commit()
                    if 'repost' in allowed:
                        known=ui.scan_destination(page,gid,pid,community_id);guard()
                        if known:
                            write_state(db,community_id,gid,pid,'already',target_post_id=known[0]['post_id'])
                        else:
                            source_ready(page,row,guard)
                            proof=ui.prepare(page,gid,pid,community_id,identity_guard=guard)
                            booked=[]
                            def before_publish():
                                budget.reserve(pid,'repost')
                                booked.append(int(datetime.now(timezone.utc).timestamp()))
                            try:
                                ui.submit(page,proof,before_publish=before_publish,identity_guard=guard)
                            finally:
                                # An attempted send always gets a real cooldown, even
                                # when its outcome is uncertain. Also separates CLI runs.
                                if booked:
                                    print('[REPOST PAUSE] sleep 20 секунд после попытки отправки',flush=True)
                                    time.sleep(20)
                            if len(booked)!=1:raise engine.StopRun('Финальная попытка не зарегистрирована; результат неизвестен')
                            matches=ui.scan_destination(page,gid,pid,community_id);guard()
                            now=int(datetime.now(timezone.utc).timestamp())
                            verified=[r for r in matches if type(r.get('published_at')) is int
                                      and booked[0]-5<=r['published_at']<=now+60]
                            if len(verified)!=1:
                                raise engine.StopRun('Репост не подтверждён точной записью на целевой стене; uncertain, повтор запрещён')
                            write_state(db,community_id,gid,pid,'verified',target_post_id=verified[0]['post_id'])
                    if 'like' in allowed:
                        source_ready(page,row,guard)
                        fresh=db.execute('SELECT * FROM posts WHERE group_id=? AND post_id=?',(gid,pid)).fetchone()
                        engine.process_one(page,db,gid,fresh,cfg['action_pause_seconds'],safety=budget,actions={'like'},already_open=True)
                    current=progress(db,run_id)
                    print(f'[COMMUNITY] {community_id}: подтверждено лайков {current["like_verified"]}/{likes}, '
                          f'репостов {current["repost_verified"]}/{reposts}')
                if not dry_run:
                    p=progress(db,run_id)
                    result='complete' if p['like_verified']>=likes and p['repost_verified']>=reposts else 'partial'
            finally:
                try:
                    if page is not None and not page.is_closed():page.close()
                finally:context.close()
    finally:
        if run_id is not None:
            db.execute('UPDATE community_runs SET finished_at=?,result=? WHERE id=?',(engine.moment(),result,run_id));db.commit()
    return {'run_id':run_id,'prepared':prepared_count,**(progress(db,run_id) if run_id is not None else {})}


def export_results(db,community_id,dest):
    rows=db.execute('SELECT * FROM community_reposts WHERE community_id=? ORDER BY group_id,post_id',(community_id,)).fetchall()
    dest=Path(dest).resolve();dest.parent.mkdir(parents=True,exist_ok=True);temp=dest.with_name(dest.name+'.tmp')
    try:
        with temp.open('w',encoding='utf-8-sig',newline='') as out:
            writer=csv.DictWriter(out,fieldnames=('community_id','source_post_url','repost_status','target_post_url','updated_at'),delimiter=';')
            writer.writeheader()
            for r in rows:writer.writerow({'community_id':community_id,'source_post_url':f'https://vk.com/wall-{r["group_id"]}_{r["post_id"]}',
                'repost_status':r['status'],'target_post_url':f'https://vk.com/wall-{community_id}_{r["target_post_id"]}' if r['target_post_id'] else '',
                'updated_at':r['updated_at']})
        os.replace(temp,dest)
    finally:temp.unlink(missing_ok=True)
    return len(rows)


def pinned_identity(account):
    bindings=accounts.load_bindings()['accounts'];record=bindings.get(str(account),{})
    vk_id=record.get('vk_id') if isinstance(record,dict) else None
    if type(vk_id) is not int or vk_id<=0:raise engine.StopRun(f'Сначала --account {account} --check-account')
    if any(key!=str(account) and isinstance(value,dict) and value.get('vk_id')==vk_id for key,value in bindings.items()):
        raise engine.StopRun('Два профиля привязаны к одному VK ID')
    return vk_id


def run_cli(args,parser,*,run_lock):
    if not (args.run_actions or args.preview or args.action_status):
        parser.error('--repost-community только с --run-actions, --preview или --action-status')
    if args.dry_run and (not args.run_actions or args.confirm):
        parser.error('--dry-run только с --run-actions и без --confirm')
    if args.test_actions is not None:parser.error('--test-actions не используется с --repost-community')
    likes,reposts=args.likes or 0,args.reposts or 0
    if args.run_actions:
        if likes<0 or reposts<1:parser.error('Для сообщества нужны --likes >=0 и --reposts >0')
        if not (args.dry_run or args.confirm):parser.error('Для отправки нужен --confirm; для проверки --dry-run')
    elif args.confirm or args.likes is not None or args.reposts is not None:
        parser.error('Числа действий и --confirm используются только с --run-actions')
    limit=args.max_posts if args.max_posts is not None else max(likes,reposts)*2 if args.run_actions else 12
    if limit<1:parser.error('--max-posts должен быть >=1')
    if args.read_pause is not None and not 5<=args.read_pause<=300:parser.error('--read-pause должен быть 5..300 секунд')
    cfg=multi.load_config(args.config);selected=multi.selected_groups(cfg,args.only,0)
    if not selected:parser.error('Нет активных исходных групп')
    if args.run_actions and ((likes and not cfg['actions']['like']) or not cfg['actions']['repost']):
        parser.error('Запрошенное действие выключено в конфигурации')
    cfg['read_pause_seconds']=max(20.,cfg['read_pause_seconds'],args.read_pause or 0.)
    vk_id=pinned_identity(args.account);profile=accounts.profile_for_account(args.account)
    if args.run_actions and not profile.is_dir():
        raise engine.StopRun('Сохранённый профиль отсутствует; сначала --check-account')
    db_path=engine.DB_PATH if args.account==1 else smoke.ACCOUNT2_DB if args.account==2 else accounts.database_for_account(args.account)
    report=args.report_file or Path(__file__).resolve().parent/f'vk_actions_results_account_{args.account}_community_{args.repost_community}.csv'
    print(f'[COMMUNITY] аккаунт={args.account}, сообщество={args.repost_community}; '
          f'режим={"без отправки" if args.dry_run else "действия" if args.run_actions else "локальный отчёт"}')
    with ExitStack() as stack:
        stack.enter_context(run_lock())
        if args.account!=1 or not accounts.uses_primary_profile_lock(args.account):stack.enter_context(accounts.exclusive_profile(args.account))
        db=stack.enter_context(closing(engine.db_open(db_path)))
        if args.account!=1:smoke.init_account2(db,vk_id)
        init_state(db,vk_id)
        try:
            if args.action_status:
                last=db.execute('SELECT * FROM community_runs WHERE community_id=? ORDER BY id DESC LIMIT 1',(args.repost_community,)).fetchone()
                print('[COMMUNITY STATUS]',dict(last) if last else 'Запусков для назначения ещё нет')
                if last:print('[COMMUNITY PROGRESS]',progress(db,last['id']))
                export_results(db,args.repost_community,report)
            elif args.preview:
                rows=select_candidates(db,selected,cfg['lookback_days'],args.repost_community,0,1,source_db=engine.DB_PATH)
                for r in rows[:limit]:print(f'[PREVIEW] https://vk.com/wall-{r["group_id"]}_{r["post_id"]} -> сообщество {args.repost_community}')
                print('[PREVIEW] Кандидатов:',len(rows))
            else:
                try:
                    run_actions(db,cfg,selected,community_id=args.repost_community,likes=likes,reposts=reposts,
                        max_posts=limit,pause=cfg['read_pause_seconds'],profile=profile,expected_vk_id=vk_id,
                        source_db=engine.DB_PATH,dry_run=args.dry_run)
                finally:
                    if not args.dry_run:
                        try:export_results(db,args.repost_community,report)
                        except OSError as exc:print('[CSV WARNING] Не удалось обновить отчёт сообщества:',type(exc).__name__)
        except engine.PlatformRateLimited as exc:
            engine.lock_on_platform_warning(db,0,str(exc));print('[COMMUNITY STOP]',exc);return 2
        except engine.StopRun as exc:
            if any(x in str(exc).lower() for x in ('captcha','капча','ограничени','робот','подозрительная активность','vk запросил вход')):
                engine.lock_on_platform_warning(db,0,str(exc))
            print('[COMMUNITY STOP]',exc);return 2
    return 0
