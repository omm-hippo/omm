import {readFile,writeFile,readdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

// Keep license texts with the compiled assets shipped in Python wheels.
const root=fileURLToPath(new URL('..',import.meta.url));
const lock=JSON.parse(await readFile(path.join(root,'package-lock.json'),'utf8'));
const sections=[];
for(const [relative,entry] of Object.entries(lock.packages).sort(([a],[b])=>a.localeCompare(b))) {
  if(!relative || entry.dev || entry.optional)continue;
  const dir=path.join(root,relative);
  const metadata=JSON.parse(await readFile(path.join(dir,'package.json'),'utf8'));
  const names=(await readdir(dir)).filter(name=>/^(licen[sc]e|copying|notice)(\.|$)/i.test(name));
  if(!names.length)throw new Error(`License text missing for ${metadata.name}; review before bundling`);
  const texts=[];
  for(const name of names.sort())texts.push(await readFile(path.join(dir,name),'utf8'));
  sections.push(`${metadata.name} ${metadata.version} (${metadata.license || entry.license || 'see below'})\n${texts.join('\n')}`);
}
await writeFile(path.join(root,'../src/omm/web/static/third-party-licenses.txt'),`Third-party licenses for OMM's local web interface\n\n${sections.join('\n\n'+'='.repeat(72)+'\n\n')}`);
console.log(`Bundled license texts for ${sections.length} runtime packages.`);
