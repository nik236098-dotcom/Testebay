import type {StoreSettings} from '@/lib/shop';
export default function ShopSocials({settings}:{settings:StoreSettings}){
 const links=[['instagram','Instagram',settings.contactInstagram],['vk','ВКонтакте',settings.contactVk],['telegram','Telegram',settings.contactTelegram]];
 return <div className="brand-socials">{links.filter(x=>x[2]).map(([id,label,url])=><a href={url} key={id} target="_blank" rel="noreferrer" aria-label={label} title={label}><img src={'/icons/'+id+'-brand.svg'} width={32} height={32} alt=""/></a>)}</div>;
}
