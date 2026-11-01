// Network-only pass-through (pages and API need the access token; nothing sensitive is cached).
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
