"""Explicit, bounded public-statistics refresh using the shared API budget.

One configured group, one wall.get head request, no pagination or actor/browser
operations. Invoking this helper is separate from rendering cached statistics.
"""
from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path

import vk_api_read as api
import vk_accounts as accounts
import vk_batch as engine
import vk_batch_engagement as engagement
import vk_groups as public_groups
import vk_statistics as stats
import vk_unified as app


def refresh_one(db_path, group_id, *, transport=None, sleeper=time.sleep,
                clock=time.time) -> dict:
    """Read at most 100 posts of an allowlisted group, safely deferring if busy.

    The activity guard covers the whole worker batch and is acquired before
    the global profile lock shared with account-1 login and catalog reads.
    Existing cursors, action attempts and actor histories are not rewritten.
    Rate limits retain VKReadClient's durable warning and raise to the caller;
    this helper never retries, unpauses, removes an existing lock or refreshes
    from a browser. API credentials are resolved locally and never returned.
    Invalid individual posts are excluded using the existing source validator;
    mixed pages report partial coverage. Invalid envelopes, duplicate IDs and
    nonempty pages with no valid observations are rejected without ingestion.
    """
    if type(group_id) is not int or group_id <= 0 or group_id not in stats._names():
        raise ValueError('group_id must belong to the configured public group allowlist')
    path=Path(db_path).resolve()
    if not path.is_file():
        raise ValueError('Existing catalog database required')
    base={'group_id':group_id,'source':api.API_SOURCE,'requested_limit':api.PAGE_SIZE}
    try:
        with accounts.exclusive_activity(path.parent),app.exclusive_run(),closing(engine.db_open(path)) as db:
            engine.require_platform_unlocked(db)
            token=api.configured_token()
            if not token:
                raise api.VkApiAuthFailure('No local API key configured')
            client=api.VKReadClient(db,token,pause_seconds=5,per_run=1,per_24h=5000,
                                    transport=transport,sleeper=sleeper,clock=clock)
            if client.remaining()<1 and not client.has_cached_wall(group_id,count=api.PAGE_SIZE,offset=0):
                return {**base,'state':'budget','observed_posts':0,'requests':client.used,'cache_hit':False}
            payload=client.wall_get(group_id,count=api.PAGE_SIZE,offset=0)
            items=payload['items'];total=payload.get('count');now=int(clock())
            ids=[item['id'] for item in items
                 if isinstance(item,dict) and type(item.get('id')) is int]
            if (len(items)>api.PAGE_SIZE or
                (total is not None and (type(total) is not int or total<len(items))) or
                len(set(ids))!=len(ids)):
                raise api.VkApiFailure('Invalid statistics head page')
            valid_items=[item for item in items if api._valid_post(item,group_id,now)]
            if items and not valid_items:
                raise api.VkApiFailure('No valid statistics head observations')
            rejected=len(items)-len(valid_items)
            public_groups.init_dates(db)
            observed_at=payload.get('_observed_at')
            if observed_at is None:observed_at=engine.moment()
            accepted=engagement._observe_page(db,group_id,valid_items,now,observed_at=observed_at)
            return {**base,'state':'partial' if rejected else 'complete',
                    'observed_posts':len(accepted),'returned_posts':len(items),'rejected_posts':rejected,
                    'requests':client.used,'cache_hit':bool(payload.get('_cache_hit',False)),
                    'observed_at':observed_at,'finished_at':engine.moment()}
    except app.RunAlreadyActive:
        return {**base,'state':'deferred','reason':'profile_or_worker_busy','observed_posts':0}
