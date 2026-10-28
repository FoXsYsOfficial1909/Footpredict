#!/usr/bin/env python3
"""Back up, compare, deploy and read back Worker modules without exposing secrets.

Content-only updates preserve all configuration. Adding the documented AI binding
uses strict inherited bindings; no existing secret value is read or copied.
"""
from __future__ import annotations

import argparse
import email.policy
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = 'footpredict-api'
SETTINGS_FIELDS = ('compatibility_date','compatibility_flags','usage_model','tags','tail_consumers','logpush','observability')


def private_write(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as target:target.write(data)


def parse_modules(body, content_type):
    if 'multipart/' not in content_type:
        raise ValueError('Expected module Worker multipart response')
    message=BytesParser(policy=email.policy.default).parsebytes(('Content-Type: '+content_type+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
    modules={}
    for part in message.iter_parts():
        name=part.get_filename() or part.get_param('name',header='content-disposition')
        if not name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Unsafe module filename')
        if name in modules:raise ValueError('Duplicate module filename')
        modules[name]=part.get_payload(decode=True)
    if 'worker.js' not in modules:raise ValueError('Expected worker.js entrypoint')
    return modules


def multipart(modules, metadata):
    boundary='footpredict-'+uuid.uuid4().hex
    fields=[('metadata',None,'application/json',json.dumps(metadata).encode())]
    fields.extend((name,name,'application/javascript+module',body) for name,body in sorted(modules.items()))
    parts=[]
    for name,filename,mime,body in fields:
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+',name):raise ValueError('Unexpected upload filename')
        disposition=f'Content-Disposition: form-data; name="{name}"'+(f'; filename="{filename}"' if filename else '')
        parts.append((f'--{boundary}\r\n{disposition}\r\nContent-Type: {mime}\r\n\r\n').encode()+body+b'\r\n')
    return b''.join(parts)+(f'--{boundary}--\r\n').encode(),'multipart/form-data; boundary='+boundary


class Client:
    def __init__(self):
        account=os.environ.get('CLOUDFLARE_ACCOUNT_ID','').strip()
        token=os.environ.get('CLOUDFLARE_API_TOKEN')
        if not re.fullmatch('[0-9a-fA-F]{32}',account) or not token:
            raise ValueError('Configured Cloudflare account/token are required')
        self.base=f'https://api.cloudflare.com/client/v4/accounts/{account}/workers/scripts/{SCRIPT}'
        self.headers={'Authorization':'Bearer '+token,'User-Agent':'FootpredictDeployment/1.1'}

    def request(self,suffix='',method='GET',body=None,content_type=None):
        headers={**self.headers}
        if content_type:headers['Content-Type']=content_type
        req=urllib.request.Request(self.base+suffix,data=body,method=method,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=60) as response:return response.read(),dict(response.headers)
        except urllib.error.HTTPError as error:
            try:data=json.load(error)
            except Exception:data={}
            messages=[{'code':x.get('code'),'message':str(x.get('message',''))[:350]} for x in data.get('errors',[])]
            raise RuntimeError(json.dumps({'http':error.code,'errors':messages},ensure_ascii=False)) from None

    def settings(self):return json.loads(self.request('/settings')[0])['result']
    def schedules(self):return json.loads(self.request('/schedules')[0])['result']
    def modules(self):
        body,headers=self.request()
        content_type=next((v for k,v in headers.items() if k.lower()=='content-type'),'')
        return parse_modules(body,content_type)


def hashes(modules):return {name:hashlib.sha256(body).hexdigest() for name,body in sorted(modules.items())}


def save_backup(client,directory):
    directory=Path(directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    settings,schedules,modules=client.settings(),client.schedules(),client.modules()
    for name,value in [('settings.json',settings),('schedules.json',schedules),('hashes.json',hashes(modules))]:
        private_write(directory/name,json.dumps(value,ensure_ascii=False,indent=2).encode())
    for name,body in modules.items():private_write(directory/'modules'/name,body)
    return settings,schedules,modules


def binding_metadata(settings,add_ai=False):
    if add_ai and any(binding['name']=='AI' and binding['type']!='ai' for binding in settings['bindings']):
        raise ValueError('The name AI is already used by a different binding; preserving it')
    metadata={key:settings[key] for key in SETTINGS_FIELDS if key in settings and settings[key] is not None}
    if settings.get('placement'):metadata['placement']=settings['placement']
    metadata['main_module']='worker.js'
    metadata['bindings']=[{'name':binding['name'],'type':'inherit'} for binding in settings['bindings']]
    if add_ai and not any(binding['name']=='AI' for binding in settings['bindings']):
        metadata['bindings'].append({'name':'AI','type':'ai'})
    return metadata


def verify_settings(before,after,added_ai=False):
    b={value['name']:value for value in before['bindings']}
    a={value['name']:value for value in after['bindings']}
    if any(a.get(name)!=binding for name,binding in b.items()):
        raise RuntimeError('An existing binding changed; inspect backup and restore before continuing')
    expected=set(b)|({'AI'} if added_ai else set())
    if set(a)!=expected:raise RuntimeError('Unexpected added or missing binding')
    if added_ai and a['AI']['type']!='ai':raise RuntimeError('Unexpected AI binding type')
    for key in SETTINGS_FIELDS+('placement',):
        if before.get(key)!=after.get(key):raise RuntimeError('Worker setting changed: '+key)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--backup-dir',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--add-ai-binding',action='store_true')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    client=Client()
    before,schedules,live=save_backup(client,args.backup_dir)
    if args.rollback:
        source=args.rollback/'modules'
        modules={str(path.relative_to(source)):path.read_bytes() for path in source.rglob('*') if path.is_file()}
        original=json.loads((args.rollback/'settings.json').read_text())
        metadata=binding_metadata(original)
        full_update=True
    else:
        modules={('worker.js' if path.name=='index.mjs' else path.name):path.read_bytes() for path in (ROOT/'worker').glob('*.mjs')}
        if set(live)-set(modules):raise RuntimeError('Live Worker has untracked modules; aborting rather than dropping them')
        metadata=binding_metadata(before,True) if args.add_ai_binding else {'main_module':'worker.js'}
        full_update=args.add_ai_binding
    print(json.dumps({'mode':'apply' if args.apply else 'dry_run','modules':list(modules),'ai_binding_requested':args.add_ai_binding,
                      'backup':str(args.backup_dir),'source_hashes':hashes(modules)},indent=2),flush=True)
    if not args.apply:return
    if hashes(client.modules())!=hashes(live) or client.settings()!=before:
        raise RuntimeError('Concurrent remote changes detected; no update sent')
    body,content_type=multipart(modules,metadata)
    suffix='?bindings_inherit=strict' if full_update else '/content'
    response,_=client.request(suffix,method='PUT',body=body,content_type=content_type)
    private_write(args.backup_dir/'upload-response.json',response)
    result=json.loads(response)
    if result.get('success') is not True:raise RuntimeError('Cloudflare did not confirm success')
    deployed=client.modules();after=client.settings()
    if hashes(deployed)!=hashes(modules):raise RuntimeError('Deployed modules do not match tested files')
    if args.rollback:
        verify_settings(original,after)
    else:verify_settings(before,after,args.add_ai_binding)
    if client.schedules()!=schedules:raise RuntimeError('Cron schedule changed unexpectedly')
    private_write(args.backup_dir/'settings-after.json',json.dumps(after,indent=2).encode())
    print('PASS: deployed modules read back byte-for-byte; existing bindings/settings and Cron schedules preserved.',flush=True)


if __name__=='__main__':
    try:main()
    except (ValueError,RuntimeError) as error:raise SystemExit(str(error))
