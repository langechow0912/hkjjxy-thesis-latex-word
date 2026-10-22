"""Local preparation and gated execution for the allowlisted Tabword API."""
import json
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from core import tabword

router = APIRouter(prefix='/api/tabword')


@router.get('')
def status():
    return tabword.catalog()


@router.get('/catalog/{operation}')
def read_catalog(operation: str, mode: str | None = None, language: str | None = None):
    try:
        values = {'presets': {'mode': mode}, 'detection_options': {'language': language}}.get(operation)
        if values is None:
            raise tabword.TabwordError('此接口不属于免费只读目录',403)
        return tabword.read_catalog(operation, {key:value for key,value in values.items() if value is not None})
    except tabword.TabwordError as exc:
        raise HTTPException(status_code=exc.status,detail=str(exc)) from None


@router.post('/{operation}')
def dispatch(operation: str, params: str = Form('{}'), submit: bool = Form(False), file: UploadFile | None = File(None)):
    try:
        try:
            values = json.loads(params)
        except ValueError:
            raise tabword.TabwordError('参数不是有效 JSON',422)
        content = None
        if file:
            content = (file.filename or '', file.file.read(20*1024*1024+1))
        if not submit:
            return tabword.dry_run(operation,values,content)
        if operation not in tabword.OPERATIONS:
            raise tabword.TabwordError('未知接口',404)
        if tabword.OPERATIONS[operation].stream:
            return StreamingResponse(tabword.stream_call(operation,values,content),media_type='text/event-stream',
                                     headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        return tabword.call(operation,values,content)
    except tabword.TabwordError as exc:
        raise HTTPException(status_code=exc.status,detail=str(exc)) from None
