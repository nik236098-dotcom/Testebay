'use client';
import {Minus,Plus,Trash2} from 'lucide-react';
import {productPrice,type Product} from '@/lib/shop';

export default function CartLines({lines,disabled=false,onChange}:{lines:(Product&{quantity:number})[];disabled?:boolean;onChange:(id:string,quantity:number)=>void}){
 return <div className="cart-lines">{lines.map(p=><div className="cart-item" key={p.id}>
  <img src={p.images[0]} alt={p.name}/>
  <div><strong>{p.name}</strong><p className="muted">{p.volume}{!p.available?' · Нет в наличии':''}</p>
   <div className="quantity"><button type="button" disabled={disabled} aria-label={'Уменьшить '+p.name} onClick={()=>onChange(p.id,p.quantity-1)}><Minus size={15}/></button><span>{p.quantity}</span><button type="button" disabled={disabled||p.quantity>=99} aria-label={'Увеличить '+p.name} onClick={()=>onChange(p.id,p.quantity+1)}><Plus size={15}/></button></div>
  </div>
  <div className="cart-item-end"><b>{productPrice(p.price*p.quantity)}</b><button type="button" className="icon" disabled={disabled} aria-label={'Удалить '+p.name} onClick={()=>onChange(p.id,0)}><Trash2 size={16}/></button></div>
 </div>)}</div>;
}
