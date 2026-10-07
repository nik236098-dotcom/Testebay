import {db,requireAdmin,sameOrigin,jsonBody,textValue,response,safe,HttpError,categoryKey} from '@/lib/server';
export async function PUT(r:Request,{params}:{params:Promise<{id:string}>}){return safe(async()=>{await requireAdmin();sameOrigin(r);const {id}=await params;const v=await jsonBody(r);const name=textValue(v.name,1,60,'Название категории').replace(/\s+/g,' ');const conflict=await db().prepare('SELECT id FROM categories WHERE name_key=? AND id<>?').bind(categoryKey(name),id).first();if(conflict)throw new HttpError(409,'Категория с таким названием уже существует.');const updatedAt=new Date().toISOString();const result=await db().prepare('UPDATE OR IGNORE categories SET name=?,name_key=?,updated_at=? WHERE id=? AND updated_at=?').bind(name,categoryKey(name),updatedAt,id,v.updatedAt||'').run();if(!result.meta.changes)throw new HttpError(409,'Категория уже изменена или название занято. Обновите список.');return response({category:{id,name,updatedAt}})})}
export async function DELETE(r:Request,{params}:{params:Promise<{id:string}>}){return safe(async()=>{
 await requireAdmin();sameOrigin(r);const {id}=await params,v=await jsonBody(r),d=db();
 const source=await d.prepare('SELECT updated_at FROM categories WHERE id=?').bind(id).first<{updated_at:string}>();
 if(!source||source.updated_at!==v.updatedAt)throw new HttpError(409,'Категория уже изменена или удалена. Обновите список.');
 const target=typeof v.moveTo==='string'?v.moveTo:'';
 if(target&&(target===id||!await d.prepare('SELECT id FROM categories WHERE id=?').bind(target).first()))throw new HttpError(400,'Выберите другую существующую категорию для переноса.');
 const count=await d.prepare('SELECT COUNT(*) AS n FROM products WHERE category=?').bind(id).first<{n:number}>();
 if(count?.n&&!target)throw new HttpError(409,'В категории есть товары. Выберите, куда их перенести перед удалением.');
 const statements=[];
 if(target)statements.push(d.prepare('UPDATE products SET category=?,updated_at=? WHERE category=? AND EXISTS (SELECT 1 FROM categories WHERE id=? AND updated_at=?) AND EXISTS (SELECT 1 FROM categories WHERE id=?)').bind(target,new Date().toISOString(),id,id,v.updatedAt,target));
 statements.push(d.prepare('DELETE FROM categories WHERE id=? AND updated_at=? AND NOT EXISTS (SELECT 1 FROM products WHERE category=?)').bind(id,v.updatedAt,id));
 const results=await d.batch(statements);if(!results.at(-1)?.meta.changes)throw new HttpError(409,'Категория или её товары изменились. Обновите список и повторите удаление.');
 return response({ok:true,moved:target?results[0].meta.changes:0});
})}
