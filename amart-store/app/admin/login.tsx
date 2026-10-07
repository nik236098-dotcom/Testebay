'use client';
import {useState} from 'react';
export default function Login({name}:{name:string}){
 const [password,setPassword]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function submit(event:React.FormEvent){event.preventDefault();setBusy(true);setError('');try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});const data=await r.json();if(!r.ok)throw new Error(data.error||'Не удалось войти.');window.location.reload()}catch(e){setError((e as Error).message);setBusy(false)}}
 return <main className="auth-screen"><a className="brand" href="/">{name}</a><h1>Вход для владельца</h1><form className="form-stack" style={{width:'min(100%,360px)'}} onSubmit={submit}><label>Пароль<input type="password" required autoComplete="current-password" maxLength={512} value={password} onChange={e=>setPassword(e.target.value)}/></label>{error&&<p role="alert" className="error-note">{error}</p>}<button className="primary" disabled={busy}>{busy?'Входим…':'Войти'}</button></form><a href="/">Вернуться в магазин</a></main>;
}
