"""User-selected browser connections, independent of historical actor numbers.

Actor IDs/bindings/SQLite stay fixed. Explicit login can attach a new persistent
profile to the matching actor, or create a separate actor for a new VK identity.
"""
from __future__ import annotations
from contextlib import closing,ExitStack
import json
from pathlib import Path

import vk_accounts as accounts


def enabled():return accounts.load_registry().get('account_flow')=='add_any'


def visible_numbers():
    return [n for n in accounts.account_numbers() if not accounts.account_record(n).get('archived',False)]


def initialize():
    """Explicit migration after external backup; no profile/history deletion."""
    with accounts.management_lock():
        registry=accounts.load_registry()
        if registry.get('account_flow')=='add_any':return registry
        if any(accounts.account_enabled(n) for n in accounts.account_numbers()):
            raise accounts.AccountError('disable_accounts_before_connection_migration')
        for number in accounts.account_numbers():
            row=accounts.account_record(number)
            registry['accounts'][str(number)]={**row,'enabled':False,'archived':True}
        registry['account_flow']='add_any'
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        import vk_authentication as authentication
        for number in accounts.account_numbers():authentication.invalidate(number)
        return registry


def connect(slot,observed_id,*,profile=None):
    if type(observed_id) is not int or not 0<observed_id<=2**53-1:raise ValueError('Invalid observed VK identity')
    import vk_authentication as authentication
    import vk_batch as engine
    with accounts.management_lock(),ExitStack() as ownership:
        if not enabled():raise accounts.AccountError('automatic_connection_not_enabled')
        registry=accounts.load_registry()
        source=accounts.account_record(slot)
        if source.get('archived') or accounts.account_enabled(slot):
            raise accounts.AccountError('account_must_be_disabled_for_login')
        current_profile=accounts.profile_for_account(slot)
        if profile is not None and Path(profile).resolve()!=current_profile.resolve():
            raise accounts.AccountError('login_profile_reassigned')
        profile=current_profile
        bindings=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']
        matches=[int(k) for k,v in bindings.items() if v.get('vk_id')==observed_id]
        if len(matches)>1:raise accounts.AccountError('duplicate_actor_identity')
        target=matches[0] if matches else slot if str(slot) not in bindings else max(accounts.account_numbers())+1
        if target>1000:raise accounts.AccountError('account_capacity_reached')
        if target in accounts.account_numbers() and accounts.account_enabled(target):
            raise accounts.AccountError('account_must_be_disabled_for_login')
        if target!=slot and target in accounts.account_numbers():
            import vk_control
            if vk_control._login_status_raw(target)['state'] in ('opening','waiting_browser','awaiting_login','ready','wrong_account'):
                raise accounts.AccountError('account_profile_busy')
            if not accounts.account_record(target).get('archived'):
                ownership.enter_context(accounts.exclusive_profile(target))
        scheduler=accounts.ROOT/'vk_scheduler.sqlite3'
        if scheduler.exists():
            with closing(__import__('sqlite3').connect(scheduler.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                if db.execute("SELECT 1 FROM scheduler_jobs WHERE account IN (?,?) AND status IN ('dispatching','attempting') LIMIT 1",(slot,target)).fetchone():
                    raise accounts.AccountError('account_operation_finishing')
        new_actor=target not in accounts.account_numbers()
        if new_actor:
            registry['accounts'][str(target)]={'account':target,'name':source['name'],'enabled':False,
                'policy':{'likes':0,'personal_reposts':0,'community_reposts':0,'communities':[]},
                'archived':True,'connection_pending':True}
        target_row=registry['accounts'].get(str(target),accounts.account_record(slot))
        # Disconnect aliases before publishing a new profile pointer. Histories
        # and immutable VK bindings remain under their original actor numbers.
        affected={slot,target}
        for key,row in list(registry['accounts'].items()):
            if int(key)!=target and (int(key)==slot or row.get('profile_dir')==profile.name):
                if accounts.account_enabled(int(key)):raise accounts.AccountError('account_must_be_disabled_for_login')
                registry['accounts'][key]={**row,'enabled':False,'archived':True,'redirect_account':target}
                affected.add(int(key))
        registry['accounts'][str(target)]={**target_row,'account':target,'name':source['name'],
            'enabled':False,'archived':True,'connection_pending':True,'profile_dir':profile.name}
        registry['accounts'][str(target)].pop('redirect_account',None)
        # Restored historical quotas must fit before any connection state is
        # published. Do not silently lower them or corrupt the next day plan.
        proposed=json.loads(json.dumps(registry))
        proposed['accounts'][str(target)].update(archived=False,connection_pending=False)
        import vk_schedule_policy as schedule
        from datetime import datetime,timezone
        try:
            cfg=accounts.future_policy(proposed)
            schedule.build_schedule(datetime.now(timezone.utc),cfg)
        except ValueError as exc:
            raise accounts.AccountError('connection_schedule_capacity') from exc
        if new_actor:accounts.prepare_account_database(target)
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        for number in affected:authentication.invalidate(number)
        if str(target) not in bindings:
            accounts.register_detected_id(accounts.BINDINGS_PATH,target,observed_id)
        db_path=accounts.database_for_account(target)
        if not db_path.exists():
            with closing(engine.db_open(db_path)):pass
        authentication.observe(target,observed_id)
        registry['accounts'][str(target)].update(archived=False,connection_pending=False)
        accounts.atomic_json(accounts.REGISTRY_PATH,registry)
        return target


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize',action='store_true',required=True)
    parser.parse_args();initialize();print('Account flow: add_any; historical actors retained')
