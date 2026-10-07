import type {StoreSettings} from '@/lib/shop';
export function DocumentBody({body}:{body:string}){
 const blocks=body.replace(/\r\n?/g,'\n').split(/\n\s*\n/).filter(x=>x.trim());
 return <div className="document-body">{blocks.map((block,i)=>{
  const lines=block.split('\n');const heading=lines[0].match(/^#{1,3}\s+(.+)$/);
  if(heading)return <section key={i}><h2>{heading[1]}</h2>{lines.length>1&&<p>{lines.slice(1).join('\n')}</p>}</section>;
  if(lines.every(line=>/^[-•]\s/.test(line)))return <ul key={i}>{lines.map((line,n)=><li key={n}>{line.replace(/^[-•]\s/,'')}</li>)}</ul>;
  return <p key={i}>{block}</p>;
 })}</div>;
}
export function SellerDetails({settings}:{settings:StoreSettings}){
 return <section className="document-seller"><h2>Реквизиты и контакты продавца</h2><dl>{[
  ['Магазин',settings.name],['Продавец',settings.contactPerson],['ИНН',settings.contactTaxId],['Адрес',settings.contactLegalAddress||settings.contactAddress]
 ].filter(x=>x[1]).map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
 {settings.contactPhone&&<div><dt>Телефон</dt><dd><a href={'tel:'+settings.contactPhone.replace(/[^\d+]/g,'')}>{settings.contactPhone}</a></dd></div>}
 {settings.contactEmail&&<div><dt>Электронная почта</dt><dd><a href={'mailto:'+settings.contactEmail}>{settings.contactEmail}</a></dd></div>}
 {(settings.contactPersonalTelegram||settings.contactTelegram)&&<div><dt>Telegram</dt><dd><a href={settings.contactPersonalTelegram||settings.contactTelegram} target="_blank" rel="noreferrer">Связаться с продавцом</a></dd></div>}
 {settings.contactWhatsApp&&<div><dt>WhatsApp</dt><dd><a href={settings.contactWhatsApp} target="_blank" rel="noreferrer">Связаться с продавцом</a></dd></div>}
 </dl></section>;
}
