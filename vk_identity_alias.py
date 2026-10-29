"""Explicit, bounded own-header alias preparation. No durable identity cache."""
from __future__ import annotations

from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime
import re
import sqlite3
import time
from urllib.parse import urljoin, urlsplit
from weakref import WeakKeyDictionary

import vk_accounts as accounts

TTL_SECONDS=300
_CACHE=WeakKeyDictionary()
_ROUTES={'feed','menu','account','profile','friends','groups','im','mail','messages','settings','search','login',
    'join','logout','notifications','away.php','share.php','al_im.php','id','club',
    'public','event','app','apps','video','videos','clips','music','audio','photos',
    'albums','wall','market','restore','support','help','services','docs','stories',
    'web','payments','jobs','topic','board','my','me'}


def _trusted_url(value):
    try:
        if type(value) is not str or any(ord(c)<33 or ord(c)==127 for c in value):return False
        parsed=urlsplit(value)
        return (parsed.scheme=='https' and parsed.hostname in accounts.VK_HOSTS and
                parsed.username is None and parsed.password is None and parsed.port is None and
                parsed.netloc.lower()==parsed.hostname)
    except (TypeError,ValueError):return False


def _alias(href):
    if (type(href) is not str or not href.startswith(('/','https://')) or
            any(ord(c)<33 or ord(c)==127 for c in href) or href.startswith('//') or
            any(c in href for c in ('?','#','\\'))):return None
    try:
        parsed=urlsplit(urljoin('https://vk.com/',href))
        if not _trusted_url(parsed.geturl()):return None
        found=re.fullmatch(r'/([A-Za-z][A-Za-z0-9_.]{0,63})',parsed.path)
        if not found:return None
        value=found[1]
        if value.lower() in _ROUTES or re.fullmatch(r'(?:id|club|public|event|app)\d+',value,re.I):return None
        return value
    except (TypeError,ValueError):return None


@dataclass(frozen=True)
class _Headers:
    # Store no Page/Context references: cache values must not keep weak keys alive.
    signature:tuple
    alias:str|None
    numeric:int|None
    invalid:bool=False


def _headers(context):
    aliases=set();numbers=set();signature=[];invalid=False;unsafe_vk_header=False
    try:
        for page in list(context.pages):
            if page.is_closed():continue
            page_url=page.url
            if (urlsplit(page_url).hostname or '').lower() not in accounts.VK_HOSTS:continue
            controls=page.locator(accounts.PROFILE_LINK);count=controls.count()
            if count==0:continue
            if count!=1:raise accounts.AccountError('identity_header_ambiguous')
            href=controls.first.get_attribute('href',timeout=2500)
            signature.append((id(page),href))
            # Keep observable VK headers in the cross-tab comparison even when
            # their page authority/transport is unsuitable for alias proof.
            unsafe_vk_header|=not _trusted_url(page_url)
            number=accounts.vk_id_from_profile_href(href)
            if number is not None:numbers.add(number);continue
            alias=_alias(href)
            if alias is None:invalid=True
            else:aliases.add(alias)
    except accounts.AccountError:
        _discard(context);raise
    except (accounts.PWTimeout,accounts.PlaywrightError,AttributeError,TypeError,ValueError):
        _discard(context)
        return _Headers((),None,None,True)
    if len(aliases)>1 or len(numbers)>1:
        _discard(context);raise accounts.AccountError('identity_header_conflict')
    if aliases and unsafe_vk_header:invalid=True
    if invalid or not signature:_discard(context)
    return _Headers(tuple(signature),next(iter(aliases),None),next(iter(numbers),None),invalid)


def _discard(context):
    try:_CACHE.pop(context,None)
    except TypeError:pass


def _cached(context,headers):
    try:record=_CACHE.get(context)
    except TypeError:return None
    if record is None:return None
    previous,number,at=record
    age=time.monotonic()-at
    if headers!=previous or headers.invalid or not headers.alias or not 0<=age<TTL_SECONDS:
        _discard(context);return None
    return number


def prepared_header_identity(page,href):
    """Pure DOM/cache lookup; never performs HTTP or prepares missing proof."""
    if _alias(href) is None:return None
    context=getattr(page,'context',None)
    if context is None:return None
    headers=_headers(context)
    if (id(page),href) not in headers.signature:return None
    return _cached(context,headers)


def prepared_context_identity(context):
    """Whether aliases require contextual proof, and that proof if still valid."""
    headers=_headers(context)
    if headers.invalid:return True,None
    if headers.alias is None:
        _discard(context);return False,None
    return True,_cached(context,headers)


def _deadline(deadline,*,wait=0,spare=20):
    if deadline is None:return
    if not isinstance(deadline,datetime) or deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError('Identity deadline must be timezone aware')
    if time.time()+wait+spare>=deadline.timestamp():
        raise accounts.IdentityPreparationDeadline('identity_prepare_deadline')


def prepare_context_identity(context,*,primary_lock_held=False,deadline=None,before_request=None):
    """Resolve a unique own-header alias once, before any durable action intent.

    The public catalog's existing global ledger and primary run lock serialize
    this one bounded read with all other reads. Header controls are checked at
    preflight, after pacing/guards before reservation, and after the response.
    """
    headers=_headers(context)
    if headers.invalid:_discard(context);return None
    if headers.alias is None:return headers.numeric
    cached=_cached(context,headers)
    if cached is not None:return cached
    _discard(context);_deadline(deadline)
    import vk_api_read as api
    import vk_unified as app
    path=accounts.ROOT/'vk_batch.sqlite3'
    if not path.is_file():raise accounts.AccountError('identity_catalog_unavailable')
    token=api.configured_token()
    if not token:raise accounts.AccountError('identity_api_key_unavailable')

    def unchanged():
        if _headers(context)!=headers:
            _discard(context)
            raise accounts.AccountError('identity_header_changed')

    def permitted():
        _deadline(deadline)
        if before_request is not None:before_request()
        _deadline(deadline)
        unchanged()
        _deadline(deadline)

    def paced_sleep(seconds):
        _deadline(deadline,wait=seconds)
        time.sleep(seconds)
        _deadline(deadline)

    lease=nullcontext() if primary_lock_held else app.exclusive_run(accounts.ROOT/'.vk-unified-running.lock')
    with lease:
        _deadline(deadline)
        try:db=sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=10)
        except (OSError,sqlite3.Error):raise accounts.AccountError('identity_catalog_unavailable') from None
        with closing(db):
            db.row_factory=sqlite3.Row
            client=api.VKReadClient(db,token,pause_seconds=5,per_run=1,per_24h=5000,
                shared_path=False,transport=lambda params:api.bounded_transport(params,timeout=20),
                sleeper=paced_sleep,clock=time.time,before_request=permitted)
            number=client.resolve_screen_name(headers.alias)
        unchanged()
        _deadline(deadline,spare=0)
        if headers.numeric is not None and headers.numeric!=number:
            raise accounts.AccountError('identity_header_conflict')
        try:_CACHE[context]=(headers,number,time.monotonic())
        except TypeError:raise accounts.AccountError('identity_context_unavailable') from None
        return number
