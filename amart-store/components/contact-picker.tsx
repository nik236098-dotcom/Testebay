'use client';
import {contactMethods,ContactMethod} from '@/lib/shop';
export default function ContactPicker({value,onChange,disabled}:{value:ContactMethod;onChange:(value:ContactMethod)=>void;disabled?:boolean}){
 return <fieldset className="contact-picker" disabled={disabled}><legend>Как с вами связаться?</legend><div className="contact-options">{contactMethods.map(method=><label key={method.id} className={'contact-option '+(value===method.id?'selected':'')}><input type="radio" name="contact-method" value={method.id} checked={value===method.id} onChange={()=>onChange(method.id)}/><span className={'contact-icon contact-icon-'+method.id}><img src={'/icons/'+method.id+(method.id==='max'?'.png':'.svg')} alt="" width={24} height={24}/></span><span>{method.label}</span></label>)}</div></fieldset>
}
