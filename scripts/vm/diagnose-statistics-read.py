"""One bounded public read; emit only numeric/boolean shape and fixed reasons."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import vk_statistics_refresh as refresh
import vk_statistics as stats
import vk_accounts as accounts
import vk_api_read as api

gid=sorted(stats._names())[0]
def transport(params):
    status,payload=api._default_transport(params)
    error=payload.get('error',{}) if isinstance(payload,dict) else {}
    response=payload.get('response',{}) if isinstance(payload,dict) else {}
    items=response.get('items',[]) if isinstance(response,dict) else []
    report={'http_status':status,'api_error_code':error.get('error_code') if type(error.get('error_code')) is int else None,
            'has_response':isinstance(response,dict) and 'items' in response,'items':len(items) if isinstance(items,list) else None}
    if isinstance(items,list):
        import time
        report['valid_posts']=sum(api._valid_post(item,gid,int(time.time())) for item in items)
        report['known_counts']={key:sum(api._engagement_count(item,key) is not None for item in items if isinstance(item,dict))
                                for key in ('likes','reposts','comments','views')}
    print(json.dumps(report),flush=True)
    return status,payload
try:
    result=refresh.refresh_one(accounts.database_for_account(1),gid,transport=transport)
    print(json.dumps(result),flush=True)
except Exception as exc:
    import traceback
    print(json.dumps({'error_type':type(exc).__name__,'api_error_code':getattr(exc,'code',None),
        'reason':'head_validation' if str(exc)=='Invalid statistics head page' else 'read_error',
        'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                  for frame in traceback.extract_tb(exc.__traceback__)[-4:]]}),flush=True)
