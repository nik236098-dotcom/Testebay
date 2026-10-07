import {env} from './runtime';
import type {StoreSettings} from './shop';

// Apply the owner's requested contact replacement once; later admin edits stay intact.
const marker='amart_contacts_20261007';
const contactUpdate:Partial<StoreSettings>={
 contactPerson:'Быкова Алия Булатовна',
 contactPhone:'+7 900 329-09-10',
 contactAddress:'Набережные Челны',
 contactPickupAddress:'Набережные Челны',
 contactLegalAddress:'Набережные Челны',
 contactTaxId:'',
 contactTelegram:'https://t.me/amart_aesthetics',
 contactPersonalTelegram:'https://t.me/amart_aesthetics',
 contactInstagram:'https://www.instagram.com/amart_aesthetics?stkn=MWdrcG9zdmJ6MzFiNg==',
 contactVk:'https://vk.ru/amart_aesthetics',
 contactWhatsApp:'https://wa.me/79003290910',
 deliveryPickupText:'Набережные Челны',
 deliveryCourierText:'По Набережным Челнам — курьером Яндекс Go. Итоговая стоимость зависит от адреса и тарифа сервиса.'
};

async function applyContactUpdate(marker:string,contactUpdate:Partial<StoreSettings>){
 const d=env.DB;if(!d)throw new Error('Store database unavailable');
 if(await d.prepare('SELECT key FROM settings WHERE key=?').bind(marker).first())return;
 // The backup, narrow merge and marker share one transaction. Concurrent requests
 // cannot repeat the replacement or overwrite subsequent changes in the admin panel.
 await d.batch([
  d.prepare("INSERT OR IGNORE INTO settings (key,value) SELECT ?,value FROM settings WHERE key='store_config' AND NOT EXISTS (SELECT 1 FROM settings WHERE key=?)").bind('before_'+marker,marker),
  d.prepare("UPDATE settings SET value=json_patch(value,?) WHERE key='store_config' AND NOT EXISTS (SELECT 1 FROM settings WHERE key=?)").bind(JSON.stringify({...contactUpdate,version:crypto.randomUUID()}),marker),
  d.prepare("INSERT OR IGNORE INTO settings (key,value) SELECT ?,? WHERE EXISTS (SELECT 1 FROM settings WHERE key='store_config')").bind(marker,new Date().toISOString())
 ]);
}

export async function ensureStoreContactUpdate(){
 await applyContactUpdate(marker,contactUpdate);
 await applyContactUpdate('amart_direct_contacts_20261007',{
  contactPersonalTelegram:'https://t.me/aliya_bylatovna',
  contactWhatsApp:'https://wa.me/79003290910'
 });
}
