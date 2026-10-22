"""Tabword documented API contracts. No upstream traffic unless explicitly enabled."""
import json
import mimetypes
import os
import re
import uuid
from pathlib import Path
from dataclasses import dataclass, asdict
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError


class TabwordError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    doc: str
    encoding: str = 'query'
    required: tuple = ()
    optional: tuple = ()
    stream: bool = False
    development: bool = False
    extensions: tuple = ()


def op(name, method, path, doc, encoding='query', required=(), optional=(), stream=False, development=False, extensions=()):
    return Operation(name, method, path, doc, encoding, tuple(required.split()) if isinstance(required,str) else required,
                     tuple(optional.split()) if isinstance(optional,str) else optional, stream, development, extensions)


OPERATIONS = {o.name: o for o in [
 op('paraphrase','POST','/api/v2/paraphrase','475259615e0','json','text preset'),
 op('paraphrase_stream','POST','/api/v2/paraphrase/stream','475259616e0','json','text preset',stream=True),
 op('paraphrase_async','POST','/api/v2/paraphrase/async','475259617e0','json','text preset'),
 op('paraphrase_task','GET','/api/v2/paraphrase/task/{task_id}','475259618e0',required='task_id'),
 op('presets','GET','/api/v2/presets','475259619e0',optional='mode'),
 op('rewrite_options','GET','/api/v2/rewrite-options','514506412e0',development=True),
 op('preset_detail','GET','/api/v2/presets/{presets_id}','475259621e0',required='presets_id'),
 op('document_paraphrase','POST','/api/v2/document/paraphrase','475259622e0','multipart',optional='preset_zh preset_en generate_diff',extensions=('.doc','.docx')),
 op('estimate_cost','POST','/api/v2/document/estimate_cost','475259623e0','multipart',optional='preset_zh',extensions=('.doc','.docx')),
 op('document_async','POST','/api/v2/document/paraphrase/async','475259624e0','multipart',optional='preset_zh preset_en generate_diff callback_url',extensions=('.doc','.docx')),
 op('document_task','GET','/api/v2/document/task/{task_id}','475259625e0',required='task_id'),
 op('detection_options','GET','/api/v2/detection/options','475259626e0',optional='language'),
 op('detect_text','POST','/api/v2/detection/check','475259627e0','json','text task_platform','detection_type platform language mode'),
 op('detect_file','POST','/api/v2/detection/check-file','475259628e0','multipart','task_platform','mode title author brand_title brand_subtitle',extensions=('.doc','.docx','.txt')),
 op('balance','GET','/api/business/keys/balance/by-key','479383971e0',development=True),
 op('templates','GET','/api/templates','503441950e0',required='owner_id',development=True),
 op('template_editor','GET','/api/public/template-editor/templates/{template_id}/editor-url','503443286e0',required='template_id owner_id',development=True),
 op('delete_template','DELETE','/api/public/template-editor/templates/{template_id}','505626429e0',required='template_id owner_id',development=True),
 op('format_check','POST','/api/public/format-check/check/report','509617506e0','multipart','template_id owner_id',development=True,extensions=('.docx',)),
 op('format_check_async','POST','/api/public/format-check/jobs','509622063e0','multipart','template_id owner_id',development=True,extensions=('.docx',)),
 op('format_check_result','GET','/api/public/format-check/jobs/{job_id}/result','509622275e0',required='job_id',optional='wait_seconds',development=True),
 op('create_sample_template','POST','/api/public/template-editor/analyze-and-create/stream','503443175e0','multipart','owner_id','school_name',True,True,('.docx',)),
 op('create_instruction_template','POST','/api/public/template-editor/instruction-analyze-and-create/stream','505625658e0','multipart','owner_id','school_name',True,True,('.pdf','.doc','.docx')),
 op('format_stream','POST','/api/public/format/stream','503444439e0','multipart','template_id owner_id',stream=True,development=True,extensions=('.docx','.pdf')),
 op('format_async','POST','/api/public/format/jobs','509616912e0','multipart','template_id owner_id',development=True,extensions=('.docx','.pdf')),
 op('format_result','GET','/api/public/format/jobs/{job_id}/result','509617179e0',required='job_id',optional='wait_seconds',development=True),
]}
FREE_CATALOG_OPERATIONS = frozenset({'presets', 'detection_options'})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward credentials to another host or replay paid submissions.


def enabled(name):
    return os.getenv(name, '').lower() in ('1', 'true')


def api_key():
    value = os.getenv('TABWORD_API_KEY', '').strip()
    if value:
        return value
    configured_path = os.getenv('TABWORD_API_KEY_FILE', '').strip()
    path = Path(configured_path) if configured_path else Path(__file__).resolve().parents[1] / '.secrets' / 'tabword.key'
    if configured_path:
        if not path.is_file():
            return ''
        try:
            return path.read_text(encoding='utf-8-sig').strip()
        except OSError as exc:
            raise RuntimeError('Cannot read configured secret file for TABWORD_API_KEY') from exc
    return path.read_text(encoding='utf-8-sig').strip() if path.is_file() else ''


def catalog():
    return {'configured': bool(api_key()),
            'catalog_reads_enabled': bool(api_key()),
            'calls_enabled': enabled('TABWORD_ENABLE_CALLS'),
            'development_enabled': enabled('TABWORD_ENABLE_DEVELOPMENT'),
            'operations': [asdict(o) for o in OPERATIONS.values()]}


def prepare(name, params=None, file=None):
    """Validate and construct a request locally. file is (filename, bytes). No credentials."""
    if name not in OPERATIONS:
        raise TabwordError('未知接口',404)
    o = OPERATIONS[name]
    if not isinstance(params, (dict, type(None))):
        raise TabwordError('参数必须是对象',422)
    values = dict(params or {})
    if set(values) - set(o.required + o.optional):
        raise TabwordError('包含未声明的接口参数',422)
    for key in o.required:
        if not isinstance(values.get(key),str) or not values[key].strip():
            raise TabwordError('缺少参数：'+key,422)
    for key,value in values.items():
        if not isinstance(value,(str,int,float,bool)) or isinstance(value,(dict,list)):
            raise TabwordError('参数必须是文本或标量：'+key,422)
    if 'text' in values and (not isinstance(values['text'],str) or not 1 <= len(values['text']) <= 50000):
        raise TabwordError('文本长度须为 1–50000 字符',422)
    if 'preset' in values and len(values['preset']) > 100:
        raise TabwordError('预设代码超过 100 字符',422)
    if name == 'presets' and values.get('mode') not in (None,'reduce_ai','reduce_duplicate','both'):
        raise TabwordError('无效改写类型',422)
    if name == 'detection_options' and values.get('language') not in (None,'zh','en'):
        raise TabwordError('语言须为 zh 或 en',422)
    if name in ('detect_text','detect_file') and values.get('mode') not in (None,'sentence','paragraph'):
        raise TabwordError('检测粒度须为 sentence 或 paragraph',422)
    if 'wait_seconds' in values:
        try:
            if str(int(values['wait_seconds'])) != str(values['wait_seconds']) or not 0 <= int(values['wait_seconds']) <= 120: raise ValueError()
            values['wait_seconds'] = int(values['wait_seconds'])
        except (ValueError,TypeError): raise TabwordError('等待秒数须为 0–120 整数',422)
    if 'generate_diff' in values:
        if str(values['generate_diff']).lower() not in ('true','false'): raise TabwordError('对比文档参数须为布尔值',422)
        values['generate_diff'] = str(values['generate_diff']).lower()
    path = o.path
    for key in re.findall(r'{(\w+)}', path):
        if values[key] in ('.','..') or any(c in values[key] for c in '/\\?#\r\n'):
            raise TabwordError('无效路径编号',422)
        path = path.replace('{'+key+'}',quote(values.pop(key),safe=''))
    group = 'TABWORD_FORMAT_BASE_URL' if o.development and name not in ('balance','rewrite_options') else 'TABWORD_BASE_URL'
    base = os.getenv(group,'https://apihub.ycjg.top' if group.endswith('FORMAT_BASE_URL') else 'https://api.tabword.com').rstrip('/')
    parts = urlsplit(base)
    if parts.scheme not in ('http','https') or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username:
        raise TabwordError('服务地址须为不含路径和凭证的 origin',503)
    if parts.scheme != 'https' and parts.hostname not in ('127.0.0.1','localhost','::1'):
        raise TabwordError('远程服务必须使用 HTTPS',503)
    body = None; headers = {}
    if o.encoding == 'multipart':
        if not file or not file[1] or len(file[1]) > 20*1024*1024:
            raise TabwordError('文件不能为空，且不能超过 20 MB',422)
        filename,content = file
        if not filename.lower().endswith(o.extensions): raise TabwordError('文件格式不受此接口支持',422)
        filename = filename.replace('\\','/').rsplit('/',1)[-1]
        if any(c in filename for c in '\r\n"'): raise TabwordError('无效文件名',422)
        boundary = 'tabword'+uuid.uuid4().hex
        chunks=[]
        for key,val in values.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{val}\r\n'.encode())
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mimetypes.guess_type(filename)[0] or "application/octet-stream"}\r\n\r\n'.encode(),content,f'\r\n--{boundary}--\r\n'.encode()])
        body=b''.join(chunks);headers['Content-Type']='multipart/form-data; boundary='+boundary
    elif file:
        raise TabwordError('此接口不接收文件',422)
    elif o.encoding == 'json':
        body=json.dumps(values,ensure_ascii=False).encode();headers['Content-Type']='application/json'
    elif values:
        path+='?'+urlencode(values)
    headers['Accept']='text/event-stream' if o.stream else 'application/json'
    return o,Request(base+path,data=body,headers=headers,method=o.method)


def dry_run(name, params=None, file=None):
    o,r=prepare(name,params,file)
    return {'submitted':False,'charged':False,'operation':name,'method':o.method,'url':r.full_url,
            'encoding':o.encoding,'response':'sse' if o.stream else 'json','development':o.development,
            'file_bytes':len(file[1]) if file else 0,'message':'本地校验通过，未请求服务商、未提交、未计费。'}


def open_call(name, params=None, file=None, catalog_read=False):
    # Includes GET: querying a successful format job triggers billing upstream.
    if not enabled('TABWORD_ENABLE_CALLS') and not (catalog_read and name in FREE_CATALOG_OPERATIONS):
        raise TabwordError('当前仅校验，不向服务商提交请求',409)
    o,r=prepare(name,params,file)
    if o.development and not enabled('TABWORD_ENABLE_DEVELOPMENT'): raise TabwordError('此接口文档标记为开发中，尚未启用',409)
    key=api_key()
    if not key or '\r' in key or '\n' in key: raise TabwordError('尚未配置独立的 Tabword API Key',503)
    r.add_header('Authorization','Bearer '+key)
    try:
        response=build_opener(NoRedirect()).open(r,timeout=150)
    except HTTPError as e:
        e.close()
        raise TabwordError(f'服务商返回 HTTP {e.code}，未自动重试',e.code if 400<=e.code<=599 else 502) from None
    except (URLError,TimeoutError,OSError):
        raise TabwordError('服务商连接中断，提交状态可能未确认；未自动重试',502) from None
    expected='text/event-stream' if o.stream else 'application/json'
    if expected not in response.headers.get('Content-Type',''):
        response.close();raise TabwordError('服务商响应类型与文档不符')
    return o,response,key


def call(name, params=None, file=None, catalog_read=False):
    if catalog_read and name not in FREE_CATALOG_OPERATIONS:
        raise TabwordError('此接口不属于免费只读目录',403)
    if name in OPERATIONS and OPERATIONS[name].stream:
        raise TabwordError('流式接口请使用 stream_call',422)
    o,response,key=open_call(name,params,file,catalog_read)
    if o.stream:
        response.close();raise TabwordError('流式接口请使用 stream_call',422)
    with response:
        payload=response.read(10*1024*1024+1)
    if len(payload)>10*1024*1024: raise TabwordError('服务商响应过大')
    try: result=json.loads(payload)
    except (ValueError,UnicodeDecodeError): raise TabwordError('服务商未返回有效 JSON') from None
    if isinstance(result,dict) and result.get('success') is False: raise TabwordError('服务商报告处理失败，请检查任务状态')
    # Balance responses may contain a masked API key; never return the actual secret.
    return json.loads(json.dumps(result,ensure_ascii=False).replace(key,'[REDACTED]'))


def read_catalog(name, params=None):
    if name not in FREE_CATALOG_OPERATIONS:
        raise TabwordError('此接口不属于免费只读目录',403)
    return call(name, params, catalog_read=True)


def stream_call(name, params=None, file=None):
    if name in OPERATIONS and not OPERATIONS[name].stream:
        raise TabwordError('此接口不是流式接口',422)
    o,response,key=open_call(name,params,file)  # Open before returning so HTTP errors remain HTTP errors.
    if not o.stream:
        response.close();raise TabwordError('此接口不是流式接口',422)
    def chunks():
        try:
            with response:
                for line in response:
                    if len(line)>1024*1024: raise TabwordError('流式消息过大')
                    yield line.replace(key.encode(),b'[REDACTED]')
        except (OSError,TabwordError):
            yield b'event: error\ndata: {"message":"Stream interrupted; do not automatically resubmit"}\n\n'
    return chunks()
