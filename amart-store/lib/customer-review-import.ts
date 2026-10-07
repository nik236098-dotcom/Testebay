import {env} from './runtime';
import {customerReviews} from './customer-reviews';

// One requested replacement with owner-supplied cards, preserving later admin edits.
export async function ensureCustomerReviews(){
 const d=env.DB;if(!d)throw new Error('Store database unavailable');
 const marker=customerReviews.version;
 if(await d.prepare('SELECT key FROM settings WHERE key=?').bind(marker).first())return;
 await d.batch([
  d.prepare("INSERT OR IGNORE INTO settings (key,value) SELECT ?,value FROM settings WHERE key='reviews_config' AND NOT EXISTS (SELECT 1 FROM settings WHERE key=?)").bind('before_'+marker,marker),
  d.prepare("INSERT INTO settings (key,value) SELECT 'reviews_config',? WHERE NOT EXISTS (SELECT 1 FROM settings WHERE key=?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(JSON.stringify(customerReviews),marker),
  d.prepare('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)').bind(marker,new Date().toISOString())
 ]);
}
