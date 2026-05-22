const CACHE_NAME = 'feedin-cache-v1';

// Ativa o Service Worker imediatamente
self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// Responde às requisições (obrigatório para o PWA ser instalável)
self.addEventListener('fetch', (event) => {
    // Ignora requisições de ferramentas externas ou extensões do navegador
    if (!event.request.url.startsWith(self.location.origin)) return;

    event.respondWith(
        fetch(event.request)
            .then((response) => {
                // Se a rede respondeu perfeitamente, apenas entrega a resposta
                return response;
            })
            .catch((err) => {
                // Se a rede falhar (ou der timeout local), tenta o cache
                return caches.match(event.request).then((cachedResponse) => {
                    if (cachedResponse) {
                        return cachedResponse;
                    }
                    // Se não tiver no cache e a rede falhou, lança um erro limpo
                    console.log('Modo offline ou lentidão local detectada para:', event.request.url);
                    return new Response('Conexão instável detectada.', {
                        status: 503,
                        statusText: 'Service Unavailable',
                        headers: new Headers({ 'Content-Type': 'text/plain' })
                    });
                });
            })
    );
});