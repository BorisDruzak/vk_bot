"""Numeric-only progress for the disposable public HTTP reader.

No URLs, arguments, exception messages, credentials or response content leave
the helper through this channel. Network observers only delegate original calls.
"""
from contextlib import contextmanager
import errno
import functools
import http.client
import json
import re
import socket
import ssl
import time
import urllib.error

PHASES={'helper_start','helper_imports','input_read','request_prepare','http_open',
        'dns','tcp_connect','tls_handshake','request_send','response_headers','response_body','json_parse'}
ERRORS={'helper_deadline','helper_start_failed','helper_exit','invalid_helper_result','dns_error',
        'access_denied','connection_refused','connection_reset','network_unreachable','tls_certificate_error',
        'tls_error','timeout','http_error','http_rate_limited','vk_rate_limited','vk_access_denied','vk_api_error',
        'transport_error','invalid_json','response_too_large','unknown'}


def classify_error(exc):
    seen=set()
    while isinstance(exc,urllib.error.URLError) and not isinstance(exc,urllib.error.HTTPError):
        if id(exc) in seen or not isinstance(exc.reason,BaseException):break
        seen.add(id(exc));exc=exc.reason
    name=type(exc).__name__;os_code=getattr(exc,'winerror',None) or getattr(exc,'errno',None)
    kind=('dns_error' if isinstance(exc,socket.gaierror) else
          'tls_certificate_error' if isinstance(exc,ssl.SSLCertVerificationError) else
          'tls_error' if isinstance(exc,ssl.SSLError) else
          'http_rate_limited' if isinstance(exc,urllib.error.HTTPError) and exc.code==429 else
          'http_error' if isinstance(exc,urllib.error.HTTPError) else
          'timeout' if isinstance(exc,TimeoutError) or os_code in (errno.ETIMEDOUT,10060) else
          'access_denied' if isinstance(exc,PermissionError) or os_code in (errno.EACCES,errno.EPERM,10013) else
          'connection_refused' if isinstance(exc,ConnectionRefusedError) or os_code in (errno.ECONNREFUSED,10061) else
          'connection_reset' if isinstance(exc,ConnectionResetError) or os_code in (errno.ECONNRESET,10054) else
          'network_unreachable' if os_code in (errno.ENETUNREACH,errno.EHOSTUNREACH,10051,10065) else
          'vk_rate_limited' if name=='VkApiLimited' else
          'invalid_json' if isinstance(exc,json.JSONDecodeError) else
          'transport_error' if isinstance(exc,(OSError,urllib.error.URLError)) else 'unknown')
    out={'read_error':kind}
    for attr,key in [('errno','errno'),('winerror','winerror'),('verify_code','tls_verify_code')]:
        value=getattr(exc,attr,None)
        if type(value) is int and -65535<=value<=2**32-1:out[key]=value
    code=getattr(exc,'code',None)
    if isinstance(exc,urllib.error.HTTPError) and type(code) is int and 100<=code<=599:out['http_status']=code
    elif name in ('VkApiFailure','VkApiLimited','VkApiAuthFailure') and type(code) is int and 0<=code<=65535:out['vk_error_code']=code
    return out


def sanitize_summary(value):
    if not isinstance(value,dict):return {}
    out={}
    if type(value.get('read_id')) is str and re.fullmatch('[0-9a-f]{32}',value['read_id']):out['read_id']=value['read_id']
    if type(value.get('read_phase')) is str and value['read_phase'] in PHASES:out['read_phase']=value['read_phase']
    if type(value.get('read_error')) is str and value['read_error'] in ERRORS:out['read_error']=value['read_error']
    for key in ('elapsed_ms','timeout_ms','phase_elapsed_ms','helper_start_ms','bytes_received'):
        v=value.get(key)
        if type(v) is int and 0<=v<=2**53-1:out[key]=v
    for key in ('errno','winerror','tls_verify_code','read_exit_code'):
        v=value.get(key)
        if type(v) is int and -65535<=v<=2**32-1:out[key]=v
    for key,lo,hi in [('http_status',100,599),('vk_error_code',0,65535)]:
        v=value.get(key)
        if type(v) is int and lo<=v<=hi:out[key]=v
    timings=value.get('read_timings_ms')
    if isinstance(timings,dict):
        out['read_timings_ms']={k:v for k,v in timings.items() if k in PHASES and type(v) is int and 0<=v<=2**53-1}
    return out


def parse_progress(raw):
    if not isinstance(raw,(bytes,str)):return []
    if isinstance(raw,bytes):raw=raw[:65536].decode('utf-8','replace')
    rows=[]
    for line in raw[:65536].splitlines():
        if not line.startswith('VKREAD '):continue
        try:v=json.loads(line[7:])
        except (ValueError,TypeError):continue
        if not isinstance(v,dict) or type(v.get('version')) is not int or v['version']!=1:continue
        if type(v.get('phase')) is not str or v['phase'] not in PHASES or v.get('state') not in ('started','completed','failed'):continue
        tick=v.get('tick_ns')
        if type(tick) is not int or not 0<tick<2**63:continue
        row={'phase':v['phase'],'state':v['state'],'tick_ns':tick,**sanitize_summary(v)}
        duration=v.get('duration_ms')
        if type(duration) is int and 0<=duration<=2**53-1:row['duration_ms']=duration
        rows.append(row)
        if len(rows)>=64:break
    return rows


def summarize(raw,started_ns,*,read_error=None):
    rows=parse_progress(raw);now=time.monotonic_ns();last=rows[-1] if rows else None
    detail={'read_phase':last['phase'] if last else 'helper_start',
            'elapsed_ms':max(0,(now-started_ns)//1_000_000),'read_timings_ms':{}}
    if rows:detail['helper_start_ms']=max(0,(rows[0]['tick_ns']-started_ns)//1_000_000)
    for row in rows:
        if 'duration_ms' in row:
            p=row['phase'];detail['read_timings_ms'][p]=detail['read_timings_ms'].get(p,0)+row['duration_ms']
    if last and last['state']=='started':detail['phase_elapsed_ms']=max(0,(now-last['tick_ns'])//1_000_000)
    if last:
        for key in ('http_status','vk_error_code','bytes_received'):
            if key in last:detail[key]=last[key]
        if last['state']=='failed':detail.update(sanitize_summary(last))
    if read_error is not None:detail['read_error']=read_error
    return sanitize_summary(detail)


class ReadTrace:
    def __init__(self,stream):
        self.stream=stream;self.last=None;self.count=0;self.context={}

    def mark(self,phase,state,**fields):
        try:
            if self.count>=64:return
            safe=sanitize_summary(fields)
            self.context.update({k:v for k,v in safe.items() if k in ('http_status','bytes_received')})
            row={'version':1,'phase':phase,'state':state,'tick_ns':time.monotonic_ns(),**self.context,**safe}
            if type(fields.get('duration_ms')) is int:row['duration_ms']=max(0,fields['duration_ms'])
            self.last=row;self.count+=1
            self.stream.write('VKREAD '+json.dumps(row,separators=(',',':'))+'\n');self.stream.flush()
        except Exception:pass

    @contextmanager
    def span(self,phase):
        start=time.monotonic_ns();self.mark(phase,'started')
        try:yield
        except Exception as exc:
            if not self.last or self.last['state']!='failed':
                self.mark(phase,'failed',duration_ms=(time.monotonic_ns()-start)//1_000_000,**classify_error(exc))
            raise
        else:self.mark(phase,'completed',duration_ms=(time.monotonic_ns()-start)//1_000_000)

    def failure(self,exc):
        detail=classify_error(exc)
        if self.last and self.last['state']=='failed' and detail['read_error']=='unknown':return
        phase=self.last['phase'] if self.last else 'helper_start'
        previous=sanitize_summary(self.last) if self.last and self.last['state']=='failed' else {}
        if previous.get('read_error')=='http_rate_limited' and detail['read_error']=='vk_rate_limited' and 'vk_error_code' not in detail:
            detail['read_error']='http_rate_limited'
        self.mark(phase,'failed',**{**previous,**detail})

    @contextmanager
    def observe_network(self):
        restored=[]
        def wrap(original,phase):
            @functools.wraps(original)
            def call(*args,**kwargs):
                with self.span(phase):return original(*args,**kwargs)
            return call
        try:
            for obj,name,phase in [(socket,'getaddrinfo','dns'),(socket.socket,'connect','tcp_connect'),
                    (ssl.SSLContext,'wrap_socket','tls_handshake'),(http.client.HTTPConnection,'send','request_send'),
                    (http.client.HTTPResponse,'begin','response_headers')]:
                try:
                    original=getattr(obj,name)
                    setattr(obj,name,wrap(original,phase));restored.append((obj,name,original))
                except (AttributeError,TypeError):pass
            yield
        finally:
            for obj,name,original in reversed(restored):setattr(obj,name,original)
