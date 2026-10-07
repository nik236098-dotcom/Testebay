'use client';
import {useEffect,useRef,useState,type FormEvent} from 'react';
import {Check,Loader2,ShoppingBag} from 'lucide-react';
import {Checkbox} from '@/components/ui/checkbox';
import ContactPicker from '@/components/contact-picker';
import DeliveryFields,{blankDelivery} from '@/components/delivery-fields';
import CartLines from '@/components/cart-lines';
import {useCart} from '@/lib/use-cart';
import {api,json} from '@/lib/client';
import {contactMethods,orderTotal,type ContactMethod,type Product,type StoreSettings} from '@/lib/shop';
import './checkout.css';

export default function Checkout({initialProducts,initialSettings}:{initialProducts:Product[];initialSettings:StoreSettings}){
 const [products,setProducts]=useState(initialProducts),[settings,setSettings]=useState(initialSettings);
 const {cart,setCart,ready}=useCart();
 const [loading,setLoading]=useState(true),[loadError,setLoadError]=useState(''),[sending,setSending]=useState(false),[error,setError]=useState(''),[success,setSuccess]=useState('');
 const [rulesAccepted,setRulesAccepted]=useState(false),[delivery,setDelivery]=useState(blankDelivery);
 const [form,setForm]=useState({name:'',phone:'',contact:'',comment:'',website:'',contactMethod:'telegram' as ContactMethod});
 const orderId=useRef(''),submitting=useRef(false);
 async function refresh(){setLoading(true);setLoadError('');try{const data=await api('/api/products');setProducts(data.products);setSettings(data.settings)}catch(e){setLoadError((e as Error).message)}finally{setLoading(false)}}
 useEffect(()=>{void refresh()},[]);
 const lines=products.filter(p=>cart[p.id]).map(p=>({...p,quantity:cart[p.id]}));
 const count=lines.reduce((sum,p)=>sum+p.quantity,0),total=lines.reduce((sum,p)=>sum+p.price*p.quantity,0);
 const unavailable=lines.some(p=>!p.available)||Object.keys(cart).some(id=>!products.some(p=>p.id===id));
 const contact=contactMethods.find(method=>method.id===form.contactMethod);
 function change(id:string,quantity:number){if(submitting.current)return;setCart(current=>{const next={...current};if(quantity<1)delete next[id];else next[id]=Math.min(99,quantity);return next})}
 async function submit(event:FormEvent<HTMLFormElement>){
  event.preventDefault();if(submitting.current||loading||loadError||!rulesAccepted||!count||unavailable)return;
  submitting.current=true;setSending(true);setError('');orderId.current ||=crypto.randomUUID();
  try{
   const result=await api('/api/orders',json('POST',{...form,contact:form.contactMethod==='whatsapp'?form.phone:form.contact,rulesAccepted,delivery,id:orderId.current,total,items:lines.map(p=>({id:p.id,quantity:p.quantity}))}));
   setSettings(result.settings);setSuccess(result.id);setCart({});
   window.scrollTo({top:0,behavior:'instant'});
  }catch(e){setError((e as Error).message)}finally{submitting.current=false;setSending(false)}
 }
 const documents=[['Оферта',settings.legalOffer],['Политика конфиденциальности',settings.legalPrivacy],['Обработка персональных данных',settings.legalDataConsent],['Передача персональных данных',settings.legalTransferConsent]].filter(([,href])=>href);
 return <div className="checkout-page">
  <header className="checkout-header"><a href="/" className="brand">{settings.name}</a><a href="/#catalog" className="checkout-back">Вернуться в магазин</a></header>
  <main className="checkout-main">
   {success?<section className="checkout-success" aria-live="polite"><div className="success-icon"><Check size={28}/></div><h1>{settings.orderSuccessTitle}</h1><p className="order-success-message">{settings.orderSuccessMessage}</p><p className="checkout-order-number">Заказ №{success.slice(0,8).toUpperCase()}</p><a href="/#catalog" className="primary">Продолжить покупки</a></section>:<>
    <div className="checkout-heading"><p className="eyebrow">Ваш заказ</p><h1>Оформление заказа</h1><p>Оставьте контакты — мы свяжемся с вами для подтверждения.</p></div>
    {!ready||loading?<div className="checkout-status" role="status"><Loader2 className="spin" size={24}/>Загружаем заказ…</div>:loadError?<div className="checkout-status"><p className="error-note" role="alert">{loadError}</p><button type="button" className="secondary" onClick={refresh}>Попробовать ещё раз</button></div>:!count&&!unavailable?<div className="checkout-status"><ShoppingBag size={40} strokeWidth={1}/><h2>Корзина пока пуста</h2><a href="/#catalog" className="primary">Перейти в каталог</a></div>:<div className="checkout-grid">
     <aside className="checkout-summary"><h2>Ваши товары</h2><CartLines lines={lines} disabled={sending} onChange={change}/>{unavailable&&<p className="error-note" role="alert">Некоторые товары уже недоступны. <button type="button" disabled={sending} onClick={()=>setCart(current=>Object.fromEntries(Object.entries(current).filter(([id])=>products.some(p=>p.id===id&&p.available))))}>Убрать их из заказа</button></p>}<div className="cart-total"><span>Итого</span><strong>{orderTotal(lines,total)}</strong></div><p className="checkout-note">Доставку, итоговую стоимость и оплату согласуем с вами лично.</p></aside>
     <form className="checkout-form form-stack" onSubmit={submit}>
      <h2>Контактные данные</h2>
      <div className="checkout-contact-fields"><label>Ваше имя<input autoComplete="given-name" required minLength={2} maxLength={100} placeholder="Как к вам обращаться" value={form.name} disabled={sending} onChange={e=>setForm({...form,name:e.target.value})}/></label><label>Номер телефона<input autoComplete="tel" type="tel" inputMode="tel" required minLength={7} maxLength={32} placeholder="+7 999 123-45-67" value={form.phone} disabled={sending} onChange={e=>setForm({...form,phone:e.target.value})}/></label></div>
      <ContactPicker value={form.contactMethod} disabled={sending} onChange={contactMethod=>setForm(previous=>({...previous,contactMethod,contact:''}))}/>
      {form.contactMethod==='whatsapp'?<p className="checkout-note">Свяжемся в WhatsApp по указанному номеру телефона.</p>:<label>{contact?.label}: контакт для связи<input autoComplete="off" required minLength={5} maxLength={160} placeholder={contact?.placeholder} value={form.contact} disabled={sending} onChange={e=>setForm({...form,contact:e.target.value})}/></label>}
      <DeliveryFields value={delivery} onChange={setDelivery} disabled={sending} pickupAddress={settings.contactPickupAddress||settings.contactAddress}/>
      <label>Комментарий <span className="muted">· необязательно</span><textarea maxLength={1000} placeholder="Пожелания к заказу или доставке" value={form.comment} disabled={sending} onChange={e=>setForm({...form,comment:e.target.value})}/></label>
      <input className="honeypot" aria-hidden="true" tabIndex={-1} autoComplete="off" value={form.website} onChange={e=>setForm({...form,website:e.target.value})}/>
      <div className="checkout-rules"><label className="check-row"><Checkbox id="checkout-rules" checked={rulesAccepted} required disabled={sending} onCheckedChange={value=>setRulesAccepted(value===true)}/><span>Я ознакомлен с правилами оформления заказа и принимаю их.</span></label><details><summary>Правила и документы</summary><p>Принимаю оферту, ознакомлен с политикой конфиденциальности и даю согласие на обработку и передачу данных для выполнения заказа.</p><nav aria-label="Документы для оформления заказа">{documents.map(([label,href])=><a key={href} href={href} target="_blank" rel="noreferrer">{label}</a>)}</nav></details></div>
      {error&&<div className="error-note" role="alert"><p>{error}</p><button type="button" disabled={sending} onClick={refresh}>Обновить данные заказа</button></div>}
      <button className="primary full checkout-submit" disabled={sending||!rulesAccepted||unavailable||!count}>{sending?<><Loader2 className="spin" size={18}/>Отправляем…</>:'Отправить заказ'}</button>
     </form>
    </div>}
   </>}
  </main>
 </div>;
}
