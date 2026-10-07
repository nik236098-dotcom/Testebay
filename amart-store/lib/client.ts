import type {LegalDocument} from './legal-documents';
import type {Product,Order,Category,Banner,StoreSettings,ReviewSettings} from './shop';
type ApiResult={documents:LegalDocument[];document:LegalDocument;products:Product[];categories:Category[];banner:Banner;settings:StoreSettings;reviews:ReviewSettings;category:Category;orders:Order[];telegramReady:boolean;url:string;id:string;notified:boolean};
export async function api<T=ApiResult>(url:string,init?:RequestInit):Promise<T>{const r=await fetch(url,{...init,cache:'no-store'});let data:Record<string,unknown>;try{data=await r.json() as Record<string,unknown>}catch{throw new Error('Нет связи с магазином. Попробуйте ещё раз.')}if(!r.ok)throw new Error(typeof data.error==='string'?data.error:'Не удалось сохранить изменения.');return data as T;}
export function json(method:string,data:unknown):RequestInit{return {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}}
