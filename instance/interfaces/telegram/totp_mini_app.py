"""Servidor local do Mini App TOTP, sem passar pelo Codex."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from skills.totp.core import TotpError
from skills.totp import vault
from mini_apps.totp.application import TotpApplication, password_hash, password_matches


HTML = """<!doctype html><html lang=pt-BR><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>TOTP</title>
<style>body{font:16px system-ui;background:#111827;color:#f9fafb;margin:0;padding:18px}main{max-width:680px;margin:auto}input,button{font:inherit;padding:11px;border-radius:8px;border:1px solid #374151}input{background:#1f2937;color:white;width:100%;box-sizing:border-box;margin:5px 0 12px}button{background:#2563eb;color:white;border:0;margin:4px;cursor:pointer}.danger{background:#b91c1c}.card{background:#1f2937;padding:14px;margin:10px 0;border-radius:12px}.code{font-size:32px;letter-spacing:5px;font-weight:700}.muted{color:#9ca3af}.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}.hidden{display:none}</style>
<main><h1>Tokens TOTP</h1><section id=auth><p id=msg>Verificando…</p><form id=login class=hidden><input id=password type=password autocomplete=current-password placeholder='Senha do TOTP' required><button>Entrar</button></form><form id=setup class=hidden><input id=newpass type=password autocomplete=new-password placeholder='Crie uma senha' required><input id=confirm type=password placeholder='Confirme a senha' required><button>Proteger</button></form></section><section id=app class=hidden><input id=search placeholder='Pesquisar issuer ou conta'><div id=tokens></div><h2>Adicionar</h2><form id=add><input id=issuer placeholder=Issuer required><input id=account placeholder=Conta required><input id=secret placeholder='Chave Base32 ou URI otpauth://totp' required><button>Adicionar</button></form><button id=qr>Adicionar por QR</button><input id=qrfile type=file accept='image/*' class=hidden><button id=change>Alterar senha</button></section></main>
<script>(async()=>{const q=s=>document.querySelector(s), init=window.Telegram?.WebApp?.initData||''; Telegram?.WebApp?.ready(); let token=''; async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':init,'X-TOTP-Session':token},body:body?JSON.stringify(body):undefined});let j=await r.json();if(!r.ok)throw Error(j.error||'Falha');return j} function show(id){['#login','#setup','#app'].forEach(x=>q(x).classList.add('hidden'));q(id).classList.remove('hidden')} async function boot(){try{let j=await api('/api/totp/session',{init_data:init});q('#msg').textContent=j.configured?'Autentique-se para continuar.':'Crie a senha do módulo TOTP.';show(j.configured?'#login':'#setup')}catch(e){q('#msg').textContent=e.message}} async function load(){let j=await api('/api/totp/tokens');q('#tokens').innerHTML=j.tokens.map(x=>`<article class=card data-key="${(x.issuer+' '+x.account).toLowerCase()}"><b>${x.issuer}</b><div class=muted>${x.account}</div><div class=code>${x.code}</div><div class=muted>${x.remaining}s restantes</div><button data-copy="${x.code}">Copiar</button><button data-edit="${x.entry}">Editar</button><button class=danger data-del="${x.entry}">Excluir</button></article>`).join('')||'<p class=muted>Nenhum token cadastrado.</p>'} q('#login').onsubmit=async e=>{e.preventDefault();try{let j=await api('/api/totp/unlock',{password:q('#password').value});token=j.session;show('#app');await load()}catch(x){q('#msg').textContent=x.message}};q('#setup').onsubmit=async e=>{e.preventDefault();try{if(q('#newpass').value!==q('#confirm').value)throw Error('As senhas não coincidem.');let j=await api('/api/totp/setup',{password:q('#newpass').value});token=j.session;show('#app');await load()}catch(x){q('#msg').textContent=x.message}};q('#add').onsubmit=async e=>{e.preventDefault();try{await api('/api/totp/tokens',{issuer:q('#issuer').value,account:q('#account').value,secret:q('#secret').value});e.target.reset();await load()}catch(x){alert(x.message)}};q('#qr').onclick=()=>q('#qrfile').click();q('#qrfile').onchange=async()=>{let f=q('#qrfile').files[0];if(!f)return;let b=await new Promise(r=>{let x=new FileReader();x.onload=()=>r(String(x.result).split(',')[1]);x.readAsDataURL(f)});try{await api('/api/totp/qr',{data:b});await load()}catch(x){alert(x.message)}};q('#tokens').onclick=async e=>{let b=e.target.closest('button');if(!b)return;if(b.dataset.copy){await navigator.clipboard.writeText(b.dataset.copy);b.textContent='Copiado';}if(b.dataset.del&&confirm('Excluir este token?')){await api('/api/totp/delete',{entry:b.dataset.del});await load()}if(b.dataset.edit){let i=prompt('Novo issuer');let a=prompt('Nova conta');if(i&&a)await api('/api/totp/edit',{entry:b.dataset.edit,issuer:i,account:a});await load()}};q('#search').oninput=e=>document.querySelectorAll('.card').forEach(c=>c.style.display=c.dataset.key.includes(e.target.value.toLowerCase())?'block':'none');q('#change').onclick=async()=>{let old=prompt('Senha atual'),n=prompt('Nova senha');if(old&&n)try{await api('/api/totp/password',{current:old,password:n});alert('Senha alterada')}catch(x){alert(x.message)}};await boot()})()</script>"""


def _password_hash(password: str) -> str:
    return password_hash(password)


def _password_ok(password: str, encoded: str | None) -> bool:
    return password_matches(password, encoded)


HTML = HTML.replace("await boot()})()", "await boot();setInterval(()=>{if(token&&!q('#app').classList.contains('hidden'))load().catch(()=>{})},1000)})()")
HTML = HTML.replace("Telegram?.WebApp?.ready()", "window.Telegram?.WebApp?.ready()")
HTML = HTML.replace(".hidden{display:none}", ".hidden{display:none}#search{max-width:320px}")
HTML = HTML.replace("id=issuer placeholder=Issuer required", "id=issuer placeholder=Issuer")
HTML = HTML.replace("id=account placeholder=Conta required", "id=account placeholder=Conta")
HTML = HTML.replace("await api('/api/totp/qr',{data:b});await load()", "let p=await api('/api/totp/qr',{data:b});if(confirm(`Adicionar ${p.issuer} — ${p.account}?`)){await api('/api/totp/qr/confirm',{});await load()}")
HTML = HTML.replace("<article class=card data-key=\"${(x.issuer+' '+x.account).toLowerCase()}\">", "<article class=card data-key=\"${(x.issuer+' '+x.account).toLowerCase()}\" data-label=\"${x.issuer} — ${x.account}\">")
HTML = HTML.replace("confirm('Excluir este token?')", "confirm('Excluir \\\"'+b.dataset.label+'\\\"? Esta ação não poderá ser desfeita.')")


class TotpMiniApp:
    def __init__(self, host: str, port: int, bot_token: str, owner_check: Callable[[int], bool], *, ttl: int = 900):
        self.host, self.port, self.bot_token, self.owner_check, self.ttl = host, port, bot_token, owner_check, ttl
        self.application = TotpApplication()
        self.sessions: dict[str, tuple[int, float]] = {}; self.qr_pending: dict[str, Any] = {}; self.lock = threading.RLock(); self.server: ThreadingHTTPServer | None = None; self.thread: threading.Thread | None = None

    def start(self) -> None:
        app = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None: return
            def _send(self, status: int, data: Any, content_type: str = 'application/json') -> None:
                raw = data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type', content_type); self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self) -> None:
                if self.path in ('/', '/totp'): self._send(200, HTML, 'text/html; charset=utf-8'); return
                self._send(404, {'error':'not_found'})
            def do_POST(self) -> None:
                try:
                    if int(self.headers.get('Content-Length','0')) > 128*1024: raise ValueError('requisição excede o limite')
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0')) or 0) or b'{}')
                    if not isinstance(body, dict): raise ValueError('corpo inválido')
                    out = app.handle(self.path, body, self.headers.get('X-TOTP-Session',''), self.headers.get('X-Telegram-Init-Data',''))
                    self._send(200, out)
                except PermissionError as e: self._send(401, {'error':str(e)})
                except (ValueError, TotpError, vault.TotpVaultError) as e: self._send(400, {'error':str(e)})
                except Exception: self._send(500, {'error':'falha interna do Mini App'})
        self.server = ThreadingHTTPServer((self.host, self.port), Handler); self.port = self.server.server_port; self.thread = threading.Thread(target=self.server.serve_forever, name='coworker-totp-mini-app', daemon=True); self.thread.start()

    def stop(self) -> None:
        if self.server: self.server.shutdown(); self.server.server_close()
        if self.thread: self.thread.join(timeout=2)
        with self.lock: self.sessions.clear(); self.qr_pending.clear()

    def _validate_init(self, value: str) -> int:
        fields = dict(urllib.parse.parse_qsl(value, keep_blank_values=True)); provided = fields.pop('hash', '')
        if not provided or not value or not fields: raise PermissionError('initData ausente')
        check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields)); key = hmac.new(b'WebAppData', self.bot_token.encode(), hashlib.sha256).digest(); expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, provided): raise PermissionError('initData inválida')
        try: auth_date = int(fields.get('auth_date','0')); user = json.loads(fields.get('user','{}')); uid = int(user.get('id',0))
        except (ValueError, TypeError, json.JSONDecodeError): raise PermissionError('initData inválida')
        if auth_date < int(time.time()) - 86400: raise PermissionError('initData expirada')
        if not uid or not self.owner_check(uid): raise PermissionError('usuário não autorizado')
        return uid

    def _session(self, value: str) -> int:
        with self.lock:
            item = self.sessions.get(value); now = time.time()
            if not item or item[1] < now: self.sessions.pop(value, None); raise PermissionError('sessão expirada')
            return item[0]

    def _new_session(self, uid: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock: self.sessions[token] = (uid, time.time() + self.ttl)
        return token

    def _tokens(self) -> list[dict[str, Any]]:
        return self.application.list_tokens()

    def handle(self, path: str, body: dict[str, Any], session: str, init_data: str) -> dict[str, Any]:
        if path == '/api/totp/session':
            uid=self._validate_init(str(body.get('init_data') or init_data)); return {'configured': self.application.configured()}
        if path in ('/api/totp/unlock', '/api/totp/setup'):
            uid = self._validate_init(init_data)
        else:
            uid=self._session(session)
        if path == '/api/totp/unlock':
            if not self.application.verify_password(str(body.get('password') or '')): raise PermissionError('senha inválida')
            return {'session':self._new_session(uid)}
        if path == '/api/totp/setup':
            self.application.setup_password(str(body.get('password') or '')); return {'session':self._new_session(uid)}
        if path == '/api/totp/tokens':
            if not {'issuer','account','secret'} <= body.keys(): raise ValueError('issuer, conta e segredo são obrigatórios')
            return {'tokens': self.application.add(issuer=str(body['issuer']), account=str(body['account']), secret=str(body['secret']))}
        if path == '/api/totp/qr':
            raw=base64.b64decode(str(body.get('data') or ''), validate=True); config=self.application.begin_qr(raw)
            with self.lock: self.qr_pending[session] = config
            return {'issuer':config.issuer,'account':config.account,'algorithm':config.algorithm,'digits':config.digits,'period':config.period}
        if path == '/api/totp/qr/confirm':
            with self.lock: config=self.qr_pending.pop(session, None)
            if config is None: raise ValueError('não há QR Code aguardando confirmação')
            return {'tokens': self.application.confirm_qr(config)}
        if path == '/api/totp/delete': return {'tokens': self.application.delete(str(body.get('entry') or ''))}
        if path == '/api/totp/edit':
            return {'tokens': self.application.edit(str(body.get('entry') or ''), issuer=str(body.get('issuer') or ''), account=str(body.get('account') or ''))}
        if path == '/api/totp/password':
            self.application.change_password(str(body.get('current') or ''), str(body.get('password') or '')); return {'ok':True}
        raise ValueError('rota desconhecida')
