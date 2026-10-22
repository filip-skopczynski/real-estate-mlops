import {copyFileSync,existsSync,mkdirSync,readdirSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root=path.dirname(fileURLToPath(import.meta.url));
const target=path.join(root,'public');
const assets=['index.html','app.js','styles.css'];
mkdirSync(target,{recursive:true});
for (const name of readdirSync(target)) {
  if (![...assets,'_headers'].includes(name)) throw new Error('Unexpected file in the public asset directory; inspect it before deploying.');
}
for (const name of assets) {
  const source=path.join(root,'..',name);
  if (!existsSync(source)) throw new Error('Canonical frontend is missing: '+name);
  copyFileSync(source,path.join(target,name));
}
writeFileSync(path.join(target,'_headers'),`/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
  Permissions-Policy: camera=(), microphone=(), geolocation=()
/index.html
  Cache-Control: no-cache
/
  Cache-Control: no-cache
`, 'utf8');
console.log('Prepared three frontend assets and response headers. Backend code, models and credentials are excluded.');
