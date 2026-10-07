import {env} from './runtime';
import {db,getStoreSettings,HttpError,textValue} from './server';
import {TelegramSettings,contactMethodLabel,productPrice,orderTotal,deliveryLabel} from './shop';

type StoredConfig={sealedToken:string;chatId:string;enabled:boolean;botUsername:string;version:string};
const configKey='telegram_config';
const encode=(bytes:Uint8Array)=>btoa(String.fromCharCode(...bytes));
const decode=(value:string)=>Uint8Array.from(atob(value),c=>c.charCodeAt(0));
async function encryptionKey(){
 const key=env.TELEGRAM_CONFIG_KEY||'';
 if(!/^[a-f0-9]{64}$/i.test(key))throw new HttpError(503,'Сохранение токена временно недоступно. Попробуйте позже.');
 return crypto.subtle.importKey('raw',Uint8Array.from(key.match(/../g)!,x=>parseInt(x,16)),{name:'AES-GCM'},false,['encrypt','decrypt']);
}
async function seal(token:string){const iv=crypto.getRandomValues(new Uint8Array(12));const data=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:new TextEncoder().encode(configKey)},await encryptionKey(),new TextEncoder().encode(token));return encode(iv)+'.'+encode(new Uint8Array(data))}
async function unseal(value:string){try{const [iv,data]=value.split('.');return new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:decode(iv),additionalData:new TextEncoder().encode(configKey)},await encryptionKey(),decode(data)))}catch{throw new HttpError(503,'Не удалось прочитать подключение Telegram. Сохраните токен заново.')}}
async function readConfig(){const row=await db().prepare('SELECT value FROM settings WHERE key=?').bind(configKey).first<{value:string}>();return {raw:row?.value,config:row?JSON.parse(row.value) as StoredConfig:null}}
function publicConfig(c:StoredConfig|null):TelegramSettings{
 const hasToken=c?!!c.sealedToken:!!env.TELEGRAM_BOT_TOKEN;
 const chatId=c?.chatId??env.TELEGRAM_CHAT_ID??'';
 return {hasToken,chatId,enabled:c?.enabled??!!(hasToken&&chatId),botUsername:c?.botUsername||'',version:c?.version||'initial',canConfigure:!!env.TELEGRAM_CONFIG_KEY};
}
export async function getTelegramSettings(){return publicConfig((await readConfig()).config)}
export async function telegramReady(){try{const c=await getTelegramSettings();return c.enabled&&c.hasToken&&!!c.chatId}catch{return false}}
async function connection(){const {config}=await readConfig();if(config)return {...config,token:config.sealedToken?await unseal(config.sealedToken):''};return {...publicConfig(null),token:env.TELEGRAM_BOT_TOKEN||''}}
async function telegramCall<T>(token:string,method:'getMe'|'sendMessage',body:Record<string,unknown>={}){
 let response:Response;
 try{response=await fetch(`https://api.telegram.org/bot${token}/${method}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(7000),redirect:'manual'})}catch{throw new HttpError(502,'Telegram не отвечает. Попробуйте ещё раз через минуту.');}
 if(response.status>=300&&response.status<400)throw new HttpError(502,'Telegram вернул некорректный ответ. Попробуйте позже.');
 let data:{ok:boolean;result:T;error_code?:number};try{data=await response.json() as typeof data}catch{throw new HttpError(502,'Telegram вернул некорректный ответ. Попробуйте позже.')}
 if(!data.ok){const code=data.error_code||response.status;if(code===401||code===404)throw new HttpError(400,'Токен бота не принят. Проверьте его в @BotFather.');if(code===403)throw new HttpError(400,'Бот не может написать вам. Откройте его в Telegram, нажмите «Запустить» и убедитесь, что он не заблокирован.');if(code===400)throw new HttpError(400,'Проверьте Telegram ID и нажмите «Запустить» в своём боте.');if(code===429)throw new HttpError(429,'Telegram просит подождать. Повторите проверку через минуту.');throw new HttpError(502,'Не удалось отправить запрос в Telegram. Попробуйте позже.')}
 return data.result;
}
export async function saveTelegramSettings(v:Record<string,unknown>){
 const {raw,config}=await readConfig(),current=publicConfig(config);
 if(v.version!==current.version)throw new HttpError(409,'Подключение изменено в другой вкладке. Обновите настройки.');
 if(typeof v.enabled!=='boolean')throw new HttpError(400,'Проверьте переключатель уведомлений.');
 const newToken=textValue(v.token??'',0,150,'Токен бота');
 const chatId=textValue(v.chatId??'',0,20,'Telegram ID');
 if(chatId&&(!/^-?[1-9]\d*$/.test(chatId)||!Number.isSafeInteger(Number(chatId))))throw new HttpError(400,'Укажите числовой Telegram ID, а не @username.');
 if(newToken&&!/^\d{5,20}:[A-Za-z0-9_-]{20,100}$/.test(newToken))throw new HttpError(400,'Проверьте токен бота из @BotFather.');
 const token=newToken||(config?.sealedToken?await unseal(config.sealedToken):env.TELEGRAM_BOT_TOKEN||'');
 if(v.enabled&&(!token||!chatId))throw new HttpError(400,'Для уведомлений укажите токен бота и Telegram ID.');
 let botUsername=current.botUsername;
 if(token&&(v.enabled||newToken)){const bot=await telegramCall<{is_bot:boolean;username?:string}>(token,'getMe');if(!bot.is_bot)throw new HttpError(400,'Этот токен не принадлежит боту.');botUsername=bot.username||'';}
 const next:StoredConfig={sealedToken:token?await seal(token):'',chatId,enabled:v.enabled,botUsername,version:crypto.randomUUID()};
 const result=raw?await db().prepare('UPDATE settings SET value=? WHERE key=? AND value=?').bind(JSON.stringify(next),configKey,raw).run():await db().prepare('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)').bind(configKey,JSON.stringify(next)).run();
 if(!result.meta.changes)throw new HttpError(409,'Подключение уже изменено. Обновите настройки.');
 return publicConfig(next);
}
export async function testTelegramSettings(version:unknown){
 const c=await connection();if(c.version!==version)throw new HttpError(409,'Настройки изменились. Обновите подключение перед проверкой.');
 if(!c.enabled||!c.token||!c.chatId)throw new HttpError(400,'Сначала сохраните и включите подключение Telegram.');
 const settings=await getStoreSettings();await telegramCall(c.token,'sendMessage',{chat_id:c.chatId,text:`${settings.name}\n\nПроверка уведомлений: подключение работает. Здесь будут появляться новые заказы.`,link_preview_options:{is_disabled:true}});
}
function orderMessage(order:Record<string,unknown>,storeName:string){
 const items=JSON.parse(String(order.items)) as {name:string;quantity:number;price:number}[];
 // Keep contact and totals intact even for unusually large orders (Telegram limit: 4096).
 const delivery=JSON.parse(String(order.delivery||'{}'));
 const deliveryText=delivery.method?`\nДоставка: ${deliveryLabel(delivery.method)}\nАдрес: ${[delivery.city,delivery.address].filter(Boolean).join(', ')||'Самовывоз'}\nСрок изготовления: ${delivery.productionConsent?'согласован':'—'}`:'';
 const details=`\nИтого: ${orderTotal(items,Number(order.total))}${deliveryText}\nИмя: ${order.name}\nТелефон: ${order.phone||'Не указан в старом заказе'}\nСвязь: ${contactMethodLabel(String(order.contact_method||''))}\nКонтакт: ${order.contact}\nКомментарий: ${order.comment||'—'}`;
 let message=`${storeName}\nНовый заказ №${String(order.id).slice(0,8).toUpperCase()}\n\n`;
 for(let i=0;i<items.length;i++){const x=items[i],line=`${x.name} × ${x.quantity} — ${productPrice(x.price*x.quantity)}\n`;if(message.length+line.length+details.length>3850){message+=`… Ещё позиций: ${items.length-i}. Полный состав — в админке.\n`;break}message+=line}
 return message+details;
}
export async function notifyOrder(id:string){
 try{
  const c=await connection();if(!c.enabled||!c.token||!c.chatId)return false;
  const order=await db().prepare('SELECT * FROM orders WHERE id=?').bind(id).first<Record<string,unknown>>();if(!order||order.notified)return !!order?.notified;
  const settings=await getStoreSettings();await telegramCall(c.token,'sendMessage',{chat_id:c.chatId,text:orderMessage(order,settings.name),link_preview_options:{is_disabled:true}});
  await db().prepare('UPDATE orders SET notified=1 WHERE id=?').bind(id).run();return true;
 }catch{console.warn('Telegram notification failed; order retained.');return false}
}
