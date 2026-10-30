"""Allowlisted diagnostics: never serialize exception text, bodies, URLs or locals."""
import json
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import vk_read_trace as read_trace

STAGES={'source','source_navigation','source_guard','source_date_read','source_date_unavailable',
        'source_date_mismatch','source_date_conflict','source_reference','destination_precheck','availability',
        'community_prepare','community_submit','community_confirm','personal_action','browser_open',
        'attempt_reservation','final_click','cooldown','cleanup','counter_refresh','scan','tick'}
GUARDS={'footer_unknown','footer_root_missing','footer_wrong_primary','footer_nested_source','footer_like_not_unique',
        'footer_like_state_unknown','footer_like_label_conflict','footer_share_not_unique','footer_action_invalid',
        'footer_scroll_failed','footer_like_already_set','pre_action_source_missing','pre_action_like_state_conflict',
        'pre_action_state_unknown','pre_action_permission_unproven'}
TYPES={'StopRun','ActionGuardError','ActionConfirmationError','ScheduledActionStopped','SourceDateUnavailable','CommunityConfirmationError','CommunityPreparationTimeout',
       'ActionTransportError',
       'PlatformRateLimited','PostUnavailable','PostTemporaryError','PostMarkupUnknown','RuntimeError','ValueError',
       'TypeError','KeyError','AttributeError','OperationalError','DatabaseError','IntegrityError','OSError',
       'TimeoutError','Error','BatchSpacingWait','RunAlreadyActive','VkApiFailure','VkApiLimited','VkApiProtection','VkApiAuthFailure','VkApiTransientRead','VkApiReadDeadline','AccountError','SafetyLimitReached'}
REASONS={*TYPES,*('guard_'+g for g in GUARDS),*('action_stopped_'+s for s in STAGES),
         'like_transport_error','repost_transport_error','action_failures_repeated',
         'like_confirmation_timeout','repost_confirmation_timeout','community_confirmation_timeout',
         'community_confirmation_ambiguous','community_prepare_timeout','vk_like_denied','vk_repost_denied','technical_before_send','technical_error_repeated'}
FILES={'vk_batch.py','vk_scheduler.py','vk_scheduler_state.py','vk_scheduled_action.py','vk_community.py','vk_action_retry.py',
       'vk_community_ui.py','vk_action_availability.py','vk_batch_engagement.py','vk_catalog_scan.py','vk_api_read.py',
       'vk_groups.py','vk_unified.py','vk_accounts.py','vk_account_smoke.py','vk_scheduler_recovery.py'}
EVENTS={'action_started','action_stage','attempt_reserved','final_click_check','cooldown_started','cooldown_finished',
        'batch_timing','stage_timing',
        'public_read_retry',
        'action_result','action_failed','network_response','network_failed','batch_started','batch_result','phase',
        'counter_progress','scheduler_failed','diagnostics_write_failed','diagnostics_event_invalid','technical_recovery','cleanup_warning',
        'public_read_finished','public_read_failed'}
PHASES={'attention','preview','waiting_window','dry_run_monitoring','waiting_profile_lock','waiting_batch','monitoring',
        'batch_action_deadline','batch_work_window_closed','batch_daily_quota_reached','batch_stop_requested',
        'batch_account_disabled','batch_day_changed','batch_spacing_deadline','batch_guard_rejected',
        'refreshing_counters','batch_already_claimed','batch_complete','batch_spacing_wait','queue_empty',
        'batch_window_or_budget_closed','counter_refresh_budget','counter_refresh_empty','counter_refresh_stopped','stopped','technical_error_deferred','technical_error_backoff',
        'reserve_counter_refresh_budget','reserve_counter_refresh_empty','reserve_counter_refresh_stopped','reserve_waiting_profile_lock',
        'counter_refresh_deadline','reserve_counter_refresh_deadline'}
NETWORK_ERRORS={'net::ERR_TIMED_OUT','net::ERR_CONNECTION_RESET','net::ERR_CONNECTION_CLOSED',
                'net::ERR_ABORTED','net::ERR_NAME_NOT_RESOLVED','net::ERR_INTERNET_DISCONNECTED','other'}


def sanitize_observation(value):
    if not isinstance(value,dict):return {}
    out={}
    mapping={'postRoot':'post_root','primaryArticle':'primary_article','likeElements':'like_elements',
             'visibleLikes':'visible_likes','targetCount':'target_count'}
    bools={'post_root','primary_article','liked','reposted','main_post_liked'}
    numbers={'like_elements','visible_likes','target_count','expected_date','actual_date'}
    for key,val in value.items():
        key=mapping.get(key,key)
        if key in bools and (type(val) is bool or val is None):out[key]=val
        if key in numbers and type(val) is int and 0<=val<=4102444800:out[key]=val
        if key in ('source','flag_source') and val in ('inline','live','conflict','none'):out['flag_source']=val
    return out


def error_chain(exc):
    out=[];seen=set()
    while exc is not None and id(exc) not in seen and len(out)<4:
        seen.add(id(exc));name=type(exc).__name__
        item={'type':name if name in TYPES else 'OtherError'}
        code=getattr(exc,'reason_code',None)
        if type(code) is str and code in REASONS:item['reason']=code
        api_code=getattr(exc,'code',None)
        if name in ('VkApiFailure','VkApiLimited','VkApiProtection','VkApiAuthFailure') and type(api_code) is int and 0<=api_code<=65535:
            item['vk_error_code']=api_code
        stage=getattr(exc,'stage',None)
        if type(stage) is str and stage in STAGES:item['stage']=stage
        frames=[];tb=exc.__traceback__
        while tb is not None:
            name=Path(tb.tb_frame.f_code.co_filename).name
            if name in FILES:frames.append({'file':name,'line':tb.tb_lineno})
            tb=tb.tb_next
        if frames:item['frames']=frames[-8:]
        read=read_trace.sanitize_summary(getattr(exc,'read_diagnostics',None))
        if read:item['public_read']=read
        out.append(item)
        exc=exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return out


def make_record(event,*,now=None,error=None,observation=None,**data):
    moment=now if isinstance(now,datetime) and now.tzinfo is not None else datetime.now(timezone.utc)
    out={'event':event if type(event) is str and event in EVENTS else 'diagnostics_event_invalid','at':moment.astimezone(timezone.utc).isoformat()}
    integers={'job','attempt_id','source_group','source_post','community','slot','elapsed_ms','timeout_ms','requests','groups','eligible_posts','unknown_counts','missing_posts',
              'read_attempt','read_attempt_limit','retry_delay_seconds'}
    for key,value in data.items():
        if key=='account' and type(value) is int and value in (1,2):out[key]=value
        elif key in integers and type(value) is int and 0<=value<=2**53-1:out[key]=value
        elif key=='http_status' and type(value) is int and 100<=value<=599:out[key]=value
        elif key=='kind' and value in ('like','personal','community'):out[key]=value
        elif key=='stage' and type(value) is str and value in STAGES:out[key]=value
        elif key=='phase' and type(value) is str and value in PHASES:out[key]=value
        elif key=='reason' and type(value) is str and value in REASONS:out[key]=value
        elif key=='outcome' and value in ('verified','already','failed','uncertain','skipped_vk','date_skipped','unavailable','ready','budget','empty','stopped'):out[key]=value
        elif key=='endpoint' and value in ('like_ui','wall_ui','likes_add','wall_repost'):out[key]=value
        elif key=='network_error' and type(value) is str and value in NETWORK_ERRORS:out[key]=value
        elif key=='request_method' and value in ('GET','POST'):out[key]=value
        elif key=='effective_window' and event=='batch_timing':
            from vk_scheduler_timing import sanitize_window
            out[key]=sanitize_window(value)
        elif key=='timing_stage' and value in ('browser_open','source_navigation','source_guard','community_prepare','click','confirmation','cooldown'):out[key]=value
        elif key=='timing_outcome' and value in ('complete','failed'):out[key]=value
        elif key=='duration_ms' and type(value) is int and 0<=value<=2**53-1:out[key]=value
    if observation is not None:out['observation']=sanitize_observation(observation)
    if error is not None:out['error_chain']=error_chain(error)
    out.update(read_trace.sanitize_summary(data))
    return out


def write(record):
    try:print('[VK EVENT]',json.dumps(record,ensure_ascii=False),flush=True)
    except OSError:pass


def emit(callback,event,**data):
    if callable(callback):
        try:callback(event,**data)
        except Exception:pass  # Telemetry must not alter the reservation/send guard.


def watch_network(page,callback):
    def endpoint(url):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ('vk.com','vk.ru','www.vk.com','www.vk.ru','api.vk.com'):return None
        return {'/al_like.php':'like_ui','/al_wall.php':'wall_ui','/method/likes.add':'likes_add','/method/wall.repost':'wall_repost'}.get(parsed.path)
    def response(value):
        try:
            route=endpoint(value.url)
            if route:emit(callback,'network_response',endpoint=route,http_status=value.status,request_method=getattr(getattr(value,'request',None),'method',None))
        except Exception:pass
    def failed(value):
        try:
            route=endpoint(value.url)
            if route:emit(callback,'network_failed',endpoint=route,network_error=value.failure if value.failure in NETWORK_ERRORS else 'other',request_method=getattr(value,'method',None))
        except Exception:pass
    for name,handler in (('response',response),('requestfailed',failed)):
        try:page.on(name,handler)
        except Exception:pass  # An unavailable observer must not become an action failure.
