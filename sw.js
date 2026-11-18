// Keeps Tal working with no signal once it has been opened (and "הורדה לאופליין" pressed) online.
const CACHE='tal-v4';
const FILES=['./','index.html','manifest.webmanifest','icon.svg'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  const url=new URL(e.request.url);
  const put=r=>{if(r&&(r.ok||r.type==='opaque')){const c=r.clone();caches.open(CACHE).then(k=>k.put(e.request,c))}return r};
  // Photos never change under the same name: cached copy first, network only the first time.
  if(url.origin===location.origin&&/\/images\/[^/]+\.jpg$/.test(url.pathname)){
    e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request).then(put)));
    return;
  }
  // Everything else: network first so updates show up, cached copy when offline.
  e.respondWith(fetch(e.request).then(put).catch(()=>caches.match(e.request,{ignoreSearch:true}).then(r=>r||caches.match('index.html'))));
});
