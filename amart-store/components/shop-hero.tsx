import type {Banner} from '@/lib/shop';
export default function ShopHero({banner,preview=false}:{banner:Banner;preview?:boolean}){
 if(!banner.enabled&&!preview)return null;
 return <section className={'store-hero'+(preview?' store-hero-preview':'')}>
  <img className="store-hero-image" src={banner.image} alt={banner.imageAlt} fetchPriority={preview?'auto':'high'}/>
  <div className="store-hero-content">
   {banner.eyebrow&&<p className="eyebrow">{banner.eyebrow}</p>}<h1>{banner.title}</h1>
   {banner.description&&<p className="store-hero-description">{banner.description}</p>}
   {banner.buttonText&&(preview?<span className="primary">{banner.buttonText}</span>:<a className="primary" href={banner.buttonLink}>{banner.buttonText}</a>)}
  </div>
 </section>;
}
