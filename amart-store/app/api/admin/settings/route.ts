import {localLegalLinks} from '@/lib/legal-documents';
import {db,requireAdmin,sameOrigin,jsonBody,textValue,response,safe,HttpError,getStoreSettings} from '@/lib/server';
import {defaultStoreSettings,storeTextGroups,contactGroups,StoreSettings} from '@/lib/shop';
export async function GET(){return safe(async()=>{await requireAdmin();return response({settings:await getStoreSettings()})})}
export async function PUT(r:Request){return safe(async()=>{
 await requireAdmin();sameOrigin(r);const v=await jsonBody(r);
 const existing=await db().prepare("SELECT value FROM settings WHERE key='store_config'").first<{value:string}>();
 const current:StoreSettings={...defaultStoreSettings,...(existing?JSON.parse(existing.value):{}),...localLegalLinks};
 const contactsOnly=v.scope==='contacts';
 const name=contactsOnly?current.name:textValue(v.name,1,60,'Название магазина').replace(/\s+/g,' ');
 const orderSuccessTitle=contactsOnly?current.orderSuccessTitle:textValue(v.orderSuccessTitle,1,100,'Заголовок после заказа');
 const orderSuccessMessage=contactsOnly?current.orderSuccessMessage:textValue(v.orderSuccessMessage,1,1200,'Сообщение после заказа');
 if(v.version!==current.version)throw new HttpError(409,'Настройки изменены в другой вкладке. Нажмите «Вернуть сохранённое» и повторите правку.');
 const settings:StoreSettings={...current,name,orderSuccessTitle,orderSuccessMessage,version:crypto.randomUUID()};
 for(const group of (contactsOnly?contactGroups:[...storeTextGroups,...contactGroups]))for(const field of group.fields){settings[field.key]=textValue(v[field.key]===undefined?current[field.key]:v[field.key],('required' in field&&field.required)?1:0,field.max,group.title+': '+field.label);}
 for(const key of ['contactTelegram','contactWhatsApp','contactVk','contactMax','contactInstagram','contactPersonalTelegram'] as const){const value=settings[key];if(value){let valid=false;try{const url=new URL(value);valid=url.protocol==='https:'&&!url.username&&!url.password}catch{}if(!valid)throw new HttpError(400,'Укажите полную ссылку на социальную сеть, начиная с https://.');}}
 if(settings.contactTaxId&&!/^(\d{10}|\d{12})$/.test(settings.contactTaxId))throw new HttpError(400,'ИНН должен содержать 10 или 12 цифр.');
 if(settings.contactEmail&&!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(settings.contactEmail))throw new HttpError(400,'Проверьте электронную почту.');
 if(settings.contactPhone&&(!/^\+?[\d\s()-]{7,32}$/.test(settings.contactPhone)||settings.contactPhone.replace(/\D/g,'').length<7))throw new HttpError(400,'Проверьте телефон магазина.');
 const result=existing?await db().prepare("UPDATE settings SET value=? WHERE key='store_config' AND value=?").bind(JSON.stringify(settings),existing.value).run():await db().prepare("INSERT OR IGNORE INTO settings (key,value) VALUES ('store_config',?)").bind(JSON.stringify(settings)).run();
 if(!result.meta.changes)throw new HttpError(409,'Настройки уже изменены. Обновите сохранённую версию.');
 return response({settings});
})}
