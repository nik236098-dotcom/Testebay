import {notFound} from 'next/navigation';
import type {Metadata} from 'next';
import {getStoreSettings} from '@/lib/server';
import {getLegalDocument} from '@/lib/legal-server';
import {isLegalSlug,legalPages,legalPath} from '@/lib/legal-documents';
import {DocumentBody,SellerDetails} from '@/components/legal-content';
export const dynamic='force-dynamic';
type Props={params:Promise<{slug:string}>};
export async function generateMetadata({params}:Props):Promise<Metadata>{
 const {slug}=await params;if(!isLegalSlug(slug))return {title:'Документ не найден'};
 try{const [doc,settings]=await Promise.all([getLegalDocument(slug),getStoreSettings()]);return {title:doc.title+' — '+settings.name}}catch{return {title:'Документы магазина'}}
}
export default async function Page({params}:Props){
 const {slug}=await params;if(!isLegalSlug(slug))notFound();
 let data;try{data=await Promise.all([getLegalDocument(slug),getStoreSettings()])}catch{return <main className="auth-screen"><h1>Документ временно недоступен</h1><p>Пожалуйста, обновите страницу немного позже.</p><a className="secondary" href="/">Вернуться в магазин</a></main>}
 const [doc,settings]=data;
 return <div className="document-page"><header className="document-header"><a className="brand" href="/">{settings.name}</a><a className="secondary" href="/">В магазин</a></header><main className="document-layout"><aside><p>Документы магазина</p><nav aria-label="Документы">{legalPages.map(p=><a key={p.slug} href={legalPath(p.slug)} aria-current={p.slug===slug?'page':undefined}>{settings.legalTitles?.[p.slug]||p.title}</a>)}</nav><a className="document-contact-link" href="/#contacts">Контакты продавца</a></aside><article className="document-paper"><p className="eyebrow">ДОКУМЕНТЫ МАГАЗИНА</p><h1>{doc.title}</h1>{doc.updatedAt&&<p className="document-date">Обновлено {new Date(doc.updatedAt).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow'})}</p>}<DocumentBody body={doc.body}/><SellerDetails settings={settings}/><a className="secondary document-return" href="/">Вернуться в магазин</a></article></main></div>;
}
