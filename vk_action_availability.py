"""Read-only, actor-page VK permission proof; absent controls prove nothing."""
from datetime import datetime,timezone
import vk_batch as engine


PERMISSION_JS=r"""({groupId,postId,stamp,kind})=>{
 const key='-'+groupId+'_'+postId;
 const roots=[...document.querySelectorAll('[data-testid="post"][data-post-id]')]
   .filter(p=>p.getAttribute('data-post-id')===key &&
     !p.closest('[data-testid="wall_comments_comment_root"]') &&
     (!p.getAttribute('data-post-nesting-lvl')||p.getAttribute('data-post-nesting-lvl')==='0'));
 if(location.protocol!=='https:'||!['vk.com','vk.ru','www.vk.com','www.vk.ru'].includes(location.hostname)||
    location.pathname!=='/wall'+key||roots.length!==1)return {state:'invalid'};
 const root=roots[0],single=document.querySelector('#page-single-post');
 const primary=single?.querySelector(':scope > div > article')||single?.querySelector(':scope > article');
 if(root.parentElement?.closest('[data-testid="post"][data-post-id]')||
    single&&primary&&!primary.contains(root))return {state:'invalid'};
 const arrays=[];
 if(Array.isArray(window.cur?.apiPrefetchCache))arrays.push(window.cur.apiPrefetchCache);
 for(const script of document.querySelectorAll('script:not([src])')){
  const raw=script.textContent||'',marker=/window\.cur\.apiPrefetchCache\s*=\s*/.exec(raw);
  if(!marker)continue;
  const body=raw.slice(marker.index+marker[0].length),end=/;\s*window\.apiPrefetchReadyResolve\s*\(\s*\)/.exec(body);
  if(!end||end.index>8000000)continue;
  try{const data=JSON.parse(body.slice(0,end.index).trim());if(Array.isArray(data))arrays.push(data);}catch(_){}
 }
 const bool=v=>v===0||v===false?false:v===1||v===true?true:null;
 const rows=[];
 for(const entries of arrays)for(const entry of entries){
  if(entry?.method!=='wall.getById')continue;
  for(const p of entry?.response?.items||[]){
   if(p?.owner_id!==-groupId||p?.id!==postId)continue;
   if(p.date!==stamp)return {state:'invalid'};
   const liked=bool(p.likes?.user_likes),reposted=bool(p.reposts?.user_reposted);
   const canLike=bool(p.likes?.can_like),canPublish=bool(p.likes?.can_publish);
   const disabled=typeof p.likes?.repost_disabled==='boolean'?p.likes.repost_disabled:null;
   if(kind==='like')rows.push({owned:liked,allowed:canLike,permission:'likes.can_like'});
   else{
    if(canPublish===true&&disabled===true)return {state:'conflict'};
    const allowed=canPublish===false||disabled===true?false:canPublish===true?true:null;
    rows.push({owned:reposted,allowed,permission:canPublish===false?'likes.can_publish':disabled===true?'likes.repost_disabled':null});
   }
  }
 }
 if(!rows.length)return {state:'unknown'};
 for(const k of ['owned','allowed']){
  const known=new Set(rows.map(r=>r[k]).filter(v=>v!==null));
  if(known.size>1)return {state:'conflict'};
 }
 if(rows.some(r=>r.owned!==false||r.allowed!==false))return {state:'unknown'};
 const permissions=new Set(rows.map(r=>r.permission));
 if(permissions.size!==1)return {state:'conflict'};
 return {state:'denied',permission:rows[0].permission};
}"""


def confirmed_denial(page,gid,pid,stamp,kind,guard):
    """Return a minimal proof only for an explicit, stable VK prohibition.

    The caller must own the correct profile and call source_ready first.
    No navigation, selectors for substitute buttons, writes or clicks occur.
    """
    if kind not in ('like','personal','community') or not callable(guard):raise ValueError('Invalid permission probe')
    if any(type(v) is not int or v<=0 for v in (gid,pid,stamp)):raise ValueError('Exact public source required')
    expected=None
    for index in range(2):
        guard()
        value=page.evaluate(PERMISSION_JS,{'groupId':gid,'postId':pid,'stamp':stamp,'kind':kind})
        if not isinstance(value,dict) or value.get('state') in ('invalid','conflict'):
            raise engine.StopRun('VK permission source is invalid or contradictory')
        if value.get('state')!='denied':
            if expected is not None:raise engine.StopRun('VK permission changed during confirmation')
            return None
        permission=value.get('permission')
        allowed={'likes.can_like'} if kind=='like' else {'likes.can_publish','likes.repost_disabled'}
        if permission not in allowed:raise engine.StopRun('Unsupported VK permission evidence')
        if expected is not None and permission!=expected:raise engine.StopRun('VK permission changed during confirmation')
        expected=permission
        if index==0:page.wait_for_timeout(350)
    guard()
    return {'reason':'vk_like_denied' if kind=='like' else 'vk_repost_denied',
            'permission':expected,'value':expected=='likes.repost_disabled',
            'observations':2,'observed_at':datetime.now(timezone.utc).isoformat()}
