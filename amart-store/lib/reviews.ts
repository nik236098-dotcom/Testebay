import {ensureReferenceContent} from './reference-content';
import {ensureCustomerReviews} from './customer-review-import';
import {customerReviewImages} from './customer-reviews';
import {db,HttpError,textValue} from './server';
import {Review,ReviewSettings,defaultReviewSettings} from './shop';
const key='reviews_config';
export async function getReviews(admin=false):Promise<ReviewSettings>{
 await ensureReferenceContent();await ensureCustomerReviews();const row=await db().prepare('SELECT value FROM settings WHERE key=?').bind(key).first<{value:string}>();
 const config:ReviewSettings=row?{...defaultReviewSettings,...JSON.parse(row.value)}:{...defaultReviewSettings,items:[]};
 return admin?config:{...config,items:config.enabled?config.items.filter(x=>x.visible):[]};
}
export async function saveReviews(v:Record<string,unknown>):Promise<ReviewSettings>{
 await ensureReferenceContent();await ensureCustomerReviews();const row=await db().prepare('SELECT value FROM settings WHERE key=?').bind(key).first<{value:string}>();
 const current:ReviewSettings=row?JSON.parse(row.value):defaultReviewSettings;
 if(v.version!==current.version)throw new HttpError(409,'Отзывы изменены в другой вкладке. Обновите сохранённую версию.');
 if(typeof v.enabled!=='boolean'||!Array.isArray(v.items)||v.items.length>30)throw new HttpError(400,'Можно добавить не больше 30 отзывов.');
 const title=textValue(v.title,1,100,'Заголовок отзывов'),seen=new Set<string>();
 const items:Review[]=[];
 for(const item of v.items){
  if(!item||typeof item!=='object')throw new HttpError(400,'Проверьте отзыв.');
  const id=textValue(item.id,36,36,'Номер отзыва');
  if(!/^[a-f0-9-]{36}$/.test(id)||seen.has(id))throw new HttpError(400,'Обновите список отзывов.');
  seen.add(id);
  const author=textValue(item.author,0,80,'Автор'),text=textValue(item.text,0,2000,'Текст отзыва'),date=textValue(item.date,0,60,'Дата'),image=textValue(item.image,0,200,'Фото отзыва');
  if(!text&&!image)throw new HttpError(400,'Добавьте текст или фотографию отзыва.');
  if(typeof item.visible!=='boolean')throw new HttpError(400,'Проверьте видимость отзыва.');
  if(image&&!customerReviewImages.has(image)&&(!/^\/api\/media\/[a-f0-9-]+\.(jpg|png|webp)$/.test(image)||!await db().prepare('SELECT key FROM uploads WHERE key=?').bind(image.slice('/api/media/'.length)).first()))throw new HttpError(400,'Загрузите фотографию отзыва ещё раз.');
  items.push({id,author,text,date,image,visible:item.visible});
 }
 const next:ReviewSettings={title,enabled:v.enabled,items,version:crypto.randomUUID()};
 const result=row?await db().prepare('UPDATE settings SET value=? WHERE key=? AND value=?').bind(JSON.stringify(next),key,row.value).run():await db().prepare('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)').bind(key,JSON.stringify(next)).run();
 if(!result.meta.changes)throw new HttpError(409,'Отзывы уже изменены. Обновите сохранённую версию.');
 return next;
}
