import {db,HttpError,textValue} from './server';
import {isLegalSlug,legalPages,initialLegalBodies,type LegalDocument,type LegalSlug} from './legal-documents';
const key=(slug:LegalSlug)=>'legal_document:'+slug;
export async function getLegalDocument(slug:string):Promise<LegalDocument>{
 if(!isLegalSlug(slug))throw new HttpError(404,'Документ не найден.');
 const row=await db().prepare('SELECT value FROM settings WHERE key=?').bind(key(slug)).first<{value:string}>();
 if(row)return JSON.parse(row.value);
 const initial:LegalDocument={slug,title:legalPages.find(p=>p.slug===slug)!.title,body:initialLegalBodies[slug],version:'initial',updatedAt:''};
 return initial;
}
export async function getLegalDocuments(){return Promise.all(legalPages.map(p=>getLegalDocument(p.slug)))}
export async function saveLegalDocument(slug:string,value:Record<string,unknown>){
 if(!isLegalSlug(slug))throw new HttpError(404,'Документ не найден.');
 const current=await getLegalDocument(slug);
 if(value.version!==current.version)throw new HttpError(409,'Документ изменён в другой вкладке. Загрузите сохранённую версию перед повторным редактированием.');
 const next:LegalDocument={slug,title:textValue(value.title,1,180,'Название документа'),body:textValue(value.body,1,100000,'Текст документа'),updatedAt:new Date().toISOString(),version:crypto.randomUUID()};
 const row=await db().prepare('SELECT value FROM settings WHERE key=?').bind(key(slug)).first<{value:string}>();
 // Compare the original version again within the update to prevent lost edits.
 const result=row?await db().prepare('UPDATE settings SET value=? WHERE key=? AND json_extract(value,\'$.version\')=?').bind(JSON.stringify(next),key(slug),value.version).run():await db().prepare('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)').bind(key(slug),JSON.stringify(next)).run();
 if(!result.meta.changes)throw new HttpError(409,'Документ уже изменён. Ваш текст сохранён в форме; загрузите актуальную версию.');
 return next;
}
