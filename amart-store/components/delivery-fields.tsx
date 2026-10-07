import {Checkbox} from '@/components/ui/checkbox';
import {Select,SelectTrigger,SelectContent,SelectItem,SelectValue} from '@/components/ui/select';
import {deliveryMethods,type Delivery} from '@/lib/shop';
export const blankDelivery:Delivery={method:'cdek',city:'',address:'',productionConsent:false};
export default function DeliveryFields({value,onChange,disabled,productionText,pickupAddress}:{pickupAddress:string;value:Delivery;onChange:(next:Delivery)=>void;disabled:boolean;productionText:string}){
 return <fieldset className="delivery-fields form-stack" disabled={disabled}><legend>Как получить заказ</legend><label>Способ доставки<Select value={value.method} disabled={disabled} onValueChange={method=>onChange({...value,method,address:'',city:method==='pickup'?'':value.city})}><SelectTrigger className="shop-select full"><SelectValue/></SelectTrigger><SelectContent>{deliveryMethods.map(m=><SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>)}</SelectContent></Select></label>
 {value.method==='pickup'?<p className="muted small">{pickupAddress?pickupAddress+". ":""}Время получения согласуем лично.</p>:<><label>Город<input required maxLength={100} autoComplete="address-level2" value={value.city} onChange={e=>onChange({...value,city:e.target.value})} placeholder="Например, Екатеринбург"/></label><label>{value.method==='cdek'?'Адрес или код пункта СДЭК':value.method==='other'?'Служба и адрес доставки':'Адрес доставки'}<input required maxLength={300} autoComplete="street-address" value={value.address} onChange={e=>onChange({...value,address:e.target.value})} placeholder={value.method==='cdek'?'Улица, дом или код пункта выдачи':value.method==='post'?'Индекс, улица, дом, квартира':'Улица, дом, квартира'}/></label></>}
 <p className="muted small">Стоимость доставки уточним до оплаты. Можно указать удобную службу в комментарии.</p>
 {productionText&&<label className="check-row"><Checkbox checked={value.productionConsent} disabled={disabled} onCheckedChange={v=>onChange({...value,productionConsent:v===true})}/><span>Я ознакомился со сроком: {productionText}</span></label>}
 </fieldset>;
}
