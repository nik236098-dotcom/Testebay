'use client';
import {useEffect,useState} from 'react';
import {Check,Loader2,RefreshCw,Send} from 'lucide-react';
import {Switch} from '@/components/ui/switch';
import {api,json} from '@/lib/client';
import type {TelegramSettings} from '@/lib/shop';
import {toast} from 'sonner';

export default function TelegramEditor({onSaved}:{onSaved:()=>Promise<void>}){
 const [config,setConfig]=useState<TelegramSettings|null>(null),[token,setToken]=useState(''),[chatId,setChatId]=useState(''),[enabled,setEnabled]=useState(true),[dirty,setDirty]=useState(false),[loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[testing,setTesting]=useState(false),[error,setError]=useState(''),[tested,setTested]=useState(false);
 function apply(c:TelegramSettings){setConfig(c);setToken('');setChatId(c.chatId);setEnabled(c.hasToken?c.enabled:true);setDirty(false);setTested(false)}
 async function load(){setLoading(true);setError('');try{const r=await api<{telegram:TelegramSettings}>('/api/admin/telegram');apply(r.telegram)}catch(e){setError((e as Error).message)}finally{setLoading(false)}}
 useEffect(()=>{load()},[]);
 useEffect(()=>{if(!dirty)return;const fn=(e:BeforeUnloadEvent)=>{e.preventDefault();e.returnValue=''};window.addEventListener('beforeunload',fn);return()=>window.removeEventListener('beforeunload',fn)},[dirty]);
 function changed(){setDirty(true);setTested(false)}
 async function save(e:React.FormEvent){e.preventDefault();if(!config||saving)return;setSaving(true);setError('');try{const r=await api<{telegram:TelegramSettings}>('/api/admin/telegram',json('PUT',{token,chatId,enabled,version:config.version}));apply(r.telegram);await onSaved();toast.success('Подключение Telegram сохранено')}catch(e){setError((e as Error).message)}finally{setSaving(false)}}
 async function test(){if(!config||dirty||testing)return;setTesting(true);setTested(false);setError('');try{await api('/api/admin/telegram',json('POST',{version:config.version}));setTested(true);toast.success('Тестовое уведомление отправлено')}catch(e){setError((e as Error).message)}finally{setTesting(false)}}
 return <div className="telegram-editor"><div className="content-heading"><h2>Уведомления в Telegram</h2><p className="muted">Бот присылает новые заказы: товары, сумму, имя, контакт покупателя и комментарий.</p></div>
 <div className="telegram-setup-note"><ol><li>Создайте бота в <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> и скопируйте его токен.</li><li>Откройте своего бота и нажмите «Запустить».</li><li>Введите токен и свой числовой Telegram ID, сохраните и отправьте тест.</li></ol><p>Ваш ID — число, не @username. Его можно узнать у <a href="https://t.me/userinfobot" target="_blank" rel="noreferrer">@userinfobot</a>.</p></div>
 {loading?<div className="empty"><Loader2 className="spin"/>Загружаем подключение…</div>:!config?<div className="error-note" role="alert">{error}<button className="secondary" onClick={load}>Повторить</button></div>:<form className="form-stack telegram-form" onSubmit={save}>
 <fieldset className="form-stack" disabled={saving||testing}>
 <div className="telegram-status"><span className="status">{config.hasToken?(config.enabled?'Уведомления включены':'Уведомления выключены'):'Бот не подключён'}</span>{config.botUsername&&<a href={'https://t.me/'+encodeURIComponent(config.botUsername)} target="_blank" rel="noreferrer">@{config.botUsername}</a>}</div>
 <label>Токен бота<input type="password" autoComplete="new-password" autoCapitalize="none" spellCheck={false} maxLength={150} value={token} onChange={e=>{setToken(e.target.value);changed()}} placeholder={config.hasToken?'Токен сохранён. Введите новый для замены':'Вставьте токен из @BotFather'} required={enabled&&!config.hasToken}/></label><p className="muted small">Сохранённый токен скрыт. Пустое поле оставит текущий токен без изменений.</p>
 <label>Telegram ID администратора<input inputMode="numeric" autoComplete="off" required={enabled} maxLength={20} value={chatId} onChange={e=>{setChatId(e.target.value);changed()}} placeholder="Например, 123456789"/></label>
 <div className="toggle-row"><div><b>Присылать новые заказы</b><p>При выключении заказы остаются в админке</p></div><Switch checked={enabled} onCheckedChange={v=>{setEnabled(v);changed()}} aria-label="Присылать новые заказы"/></div>
 </fieldset>
 {!config.canConfigure&&<p className="error-note">Сохранение токена временно недоступно. Обновите страницу позже.</p>}{error&&<p className="error-note" role="alert">{error}</p>}
 <div className="telegram-actions"><button className="primary" disabled={saving||testing||!config.canConfigure||(!dirty&&config.hasToken)}>{saving?<Loader2 className="spin" size={17}/>:<Check size={17}/>} {saving?'Проверяем и сохраняем…':'Сохранить подключение'}</button><button type="button" className="secondary" disabled={saving||testing||dirty||!config.hasToken||!config.enabled} onClick={test}>{testing?<Loader2 className="spin" size={17}/>:<Send size={17}/>}Отправить тест</button><button type="button" className="secondary" disabled={saving||testing} onClick={load}><RefreshCw size={16}/>Вернуть сохранённое</button></div>
 <p className="muted small" aria-live="polite">{dirty?'Сначала сохраните изменения, затем отправьте тест.':tested?'Тест отправлен. Проверьте сообщение в Telegram.':'Проверка отправит одно сообщение на сохранённый Telegram ID.'}</p>
 <p className="muted small">Если Telegram недоступен, заказ сохранится во вкладке «Заказы». Там можно повторить отправку уведомления.</p>
 </form>}</div>
}
