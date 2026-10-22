import {readFileSync,writeFileSync,renameSync,existsSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const directory=path.dirname(fileURLToPath(import.meta.url));
const [id,account]=process.argv.slice(2);
if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id??'') || !/^[a-f0-9]{32}$/i.test(account??'')) {
  throw new Error('Usage: node configure.mjs ACTUAL_D1_DATABASE_UUID ACTUAL_CLOUDFLARE_ACCOUNT_ID');
}
const config=JSON.parse(readFileSync(path.join(directory,'wrangler.jsonc'),'utf8'));
config.account_id=account;
config.d1_databases[0].database_id=id;
const filename=path.join(directory,'wrangler.account.json');
if (existsSync(filename)) {
  const current=JSON.parse(readFileSync(filename,'utf8'));
  if (current.account_id!==account || current.d1_databases?.[0]?.database_id!==id) throw new Error('A different deployment is already configured; review it before changing account or database.');
}
writeFileSync(filename+'.tmp',JSON.stringify(config,null,2)+'\n');
renameSync(filename+'.tmp',filename);
console.log('Configured the account and D1 IDs in ignored wrangler.account.json; no credentials were saved.');
