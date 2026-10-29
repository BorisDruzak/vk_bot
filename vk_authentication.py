"""Account/profile-specific evidence of a fresh observed VK header identity.

Saved bindings alone never establish authentication. No cookies or DOM stored.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import socket
from datetime import datetime,timezone

import vk_accounts as accounts

FRESH_SECONDS=300


def evidence_path(account):
    accounts.profile_for_account(account)
    return accounts.ROOT/f'.vk-authentication-{account}.json'


def _machine():
    return hashlib.sha256((socket.gethostname()+'|'+str(Path.home())+'|'+
                           os.environ.get('USERNAME','')).encode()).hexdigest()


def _identities(account,observed):
    bindings=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts']
    expected=bindings.get(str(account),{}).get('vk_id')
    if observed is None:return expected,'not_authenticated','header_identity_missing',False
    duplicate=any(k!=str(account) and isinstance(row,dict) and row.get('vk_id')==observed
                  for k,row in bindings.items())
    if duplicate or (expected is not None and expected!=observed):
        return expected,'wrong_account','vk_identity_mismatch',False
    if expected is None:return None,'unverified','identity_not_bound',True
    return expected,'matched','exact_header_identity',True


def observe(account,observed_id,*,now=None):
    accounts.profile_for_account(account)
    if observed_id is not None and (type(observed_id) is not int or not 0<observed_id<=2**53-1):
        raise ValueError('Invalid observed identity')
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('Aware observation time required')
    expected,state,reason,can_finish=_identities(account,observed_id)
    record={'version':1,'account':account,'profile':accounts.profile_for_account(account).name,
            'machine':_machine(),'expected_vk_id':expected,'observed_vk_id':observed_id,
            'state':state,'reason':reason,'can_finish':can_finish,'checked_at':now.isoformat()}
    accounts.atomic_json(evidence_path(account),record)
    return status(account,now=now)


def invalidate(account):
    """A newly requested inspection supersedes the previous observation."""
    expected=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts'].get(str(account),{}).get('vk_id')
    accounts.atomic_json(evidence_path(account),{'version':1,'account':account,
        'profile':accounts.profile_for_account(account).name,'machine':_machine(),
        'expected_vk_id':expected,'invalidated':True,
        'checked_at':datetime.now(timezone.utc).isoformat()})


def status(account,*,now=None):
    import json
    expected=accounts.load_bindings(accounts.BINDINGS_PATH)['accounts'].get(str(account),{}).get('vk_id')
    base={'state':'unverified','expected_vk_id':expected,'observed_vk_id':None,
          'checked_at':None,'fresh':False,'can_finish':False,'reason':'no_fresh_identity_evidence'}
    path=evidence_path(account)
    if not path.exists():return base
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        checked=datetime.fromisoformat(record['checked_at'])
        now=now or datetime.now(timezone.utc)
        age=(now-checked).total_seconds()
        if (record.get('version')!=1 or record.get('account')!=account or
                record.get('profile')!=accounts.profile_for_account(account).name or
                record.get('machine')!=_machine() or record.get('expected_vk_id')!=expected or age< -5):return base
        if record.get('invalidated') is True:return base
        observed=record.get('observed_vk_id')
        if observed is not None and (type(observed) is not int or not 0<observed<=2**53-1):return base
        _,state,reason,can_finish=_identities(account,observed)
        result={**base,'observed_vk_id':observed,'checked_at':record['checked_at'],
                'state':state,'reason':reason,'fresh':age<=FRESH_SECONDS,'can_finish':can_finish and age<=FRESH_SECONDS}
        if age>FRESH_SECONDS:result.update(state='stale',reason='identity_evidence_stale')
        return result
    except (OSError,ValueError,TypeError,KeyError):return base


def may_finish(account):return status(account)['can_finish']


def require_matched(account):
    row=status(account)
    if row['state']!='matched' or not row['fresh']:
        raise accounts.AccountError('account_authentication_required')
