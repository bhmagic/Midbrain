"""Standalone local hardware-development web application."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request
import argparse
import json
import mimetypes
import sys
import webbrowser

from .collision import CalibrationCollisionGuard
from .kinematics import RebotKinematics


class ProviderClient:
    def __init__(self,base_url:str): self.base_url=base_url.rstrip('/')
    def call(self,method:str,path:str,payload:dict[str,Any]|None=None,timeout:float=120.0):
        data=None if payload is None else json.dumps(payload).encode('utf-8')
        req=request.Request(self.base_url+path,data=data,method=method,headers={'Content-Type':'application/json'})
        try:
            with request.urlopen(req,timeout=timeout) as response: raw=response.read()
        except Exception as error:
            body=getattr(error,'read',lambda:b'')()
            raise RuntimeError(body.decode('utf-8','replace') or str(error)) from error
        return {} if not raw else json.loads(raw)
    def get(self,path): return self.call('GET',path)
    def post(self,path,payload=None,timeout=120): return self.call('POST',path,payload or {},timeout)


class CalibrationApplication:
    def __init__(self,provider_url:str,collision_path:str,host:str,port:int,open_browser:bool):
        self.provider=ProviderClient(provider_url); self.host=host; self.port=port; self.open_browser=open_browser
        self.model=self.provider.get('/v1/arm/model'); self.kinematics=RebotKinematics(self.model)
        self.guard=CalibrationCollisionGuard.load(self.kinematics,collision_path)
        self.web_root=Path(__file__).resolve().parent/'calibration_web'; self.server=None

    def serve(self):
        handler=self._handler(); self.server=ThreadingHTTPServer((self.host,self.port),handler); self.server.daemon_threads=True
        url=f'http://{self.host}:{self.port}/'; print(f'Hardware Development GUI: {url}',flush=True)
        if self.open_browser: webbrowser.open(url)
        try: self.server.serve_forever()
        except KeyboardInterrupt: pass
        finally: self.server.server_close()

    def _handler(self):
        app=self
        class Handler(BaseHTTPRequestHandler):
            server_version='RebotHardwareDevelopmentGUI/0.1'
            def log_message(self,format,*args):
                # Routine successful HTTP requests are intentionally silent.
                try:
                    status=int(args[1]) if len(args)>1 else 200
                except (TypeError,ValueError):
                    status=200
                if status>=400:
                    print(f'[calibration-gui] {format%args}')
                if self.path in {'/api/state','/api/collision/check','/api/renew'}:
                    return
                print(f'[calibration-gui] {format%args}')
            def _json(self,status,payload):
                data=json.dumps(payload).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            def _body(self):
                length=int(self.headers.get('Content-Length','0')); return {} if length==0 else json.loads(self.rfile.read(length))
            def _static(self,path):
                name='index.html' if path in {'','/'} else path.lstrip('/'); target=(app.web_root/name).resolve()
                if app.web_root.resolve() not in target.parents and target!=app.web_root.resolve(): return self._json(403,{'error':'forbidden'})
                if not target.is_file(): return self._json(404,{'error':'not found'})
                data=target.read_bytes(); self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(str(target))[0] or 'application/octet-stream'); self.send_header('Cache-Control','no-store, max-age=0'); self.send_header('Pragma','no-cache'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            def do_GET(self):
                try:
                    if self.path=='/api/model': return self._json(200,app.model)
                    if self.path=='/api/state': return self._json(200,app.provider.get('/v1/arm/state'))
                    if self.path.startswith('/api/'): return self._json(404,{'error':'not found'})
                    return self._static(self.path.split('?',1)[0])
                except Exception as error: return self._json(500,{'error':str(error)})
            def do_POST(self):
                try:
                    body=self._body()
                    if self.path=='/api/lease': return self._json(200,app.provider.post('/v1/calibration/lease',body))
                    if self.path=='/api/renew': return self._json(200,app.provider.post('/v1/calibration/lease/renew',body))
                    if self.path=='/api/command': return self._json(200,app.provider.post('/v1/calibration/command',body))
                    if self.path=='/api/gravity-float': return self._json(200,app.provider.post('/v1/calibration/gravity-float',body))
                    if self.path=='/api/safe-home': return self._json(200,app.provider.post('/v1/calibration/safe-home',body,60))
                    if self.path=='/api/collision/check':
                        state=body['positions_rad']; return self._json(200,app.guard.check(state,float(body['table_height_m']),float(body['table_clearance_m'])).to_dict())
                    return self._json(404,{'error':'not found'})
                except Exception as error: return self._json(500,{'error':str(error)})
        return Handler


def main(argv=None):
    parser=argparse.ArgumentParser(description='reBot Arm standalone hardware-development GUI')
    parser.add_argument('--provider-url',default='http://127.0.0.1:8791')
    parser.add_argument('--collision-config',required=True); parser.add_argument('--host',default='127.0.0.1'); parser.add_argument('--port',type=int,default=8792)
    parser.add_argument('--no-browser',action='store_true'); args=parser.parse_args(argv)
    CalibrationApplication(args.provider_url,args.collision_config,args.host,args.port,not args.no_browser).serve(); return 0

if __name__=='__main__': raise SystemExit(main())
