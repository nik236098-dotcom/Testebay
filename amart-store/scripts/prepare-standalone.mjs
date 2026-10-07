import {cp,mkdir} from 'node:fs/promises';
await cp('public','.next/standalone/public',{recursive:true});
await cp('.next/static','.next/standalone/.next/static',{recursive:true});
await cp('drizzle','.next/standalone/drizzle',{recursive:true});
await mkdir('.next/standalone/scripts',{recursive:true});
await cp('scripts/backup.mjs','.next/standalone/scripts/backup.mjs');
