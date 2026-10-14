const CACHE_NAME = 'feedin-cache-v1';

// Ativa o Service Worker imediatamente
self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// Responde às requisições
self.addEventListener('fetch', (event) => {
    // 1. Ignora requisições de origens externas ou extensões
    if (!event.request.url.startsWith(self.location.origin)) return;

    // 2. Não intercepta requisições POST, PUT, DELETE (apenas GET)
    if (event.request.method !== 'GET') return;

    const url = new URL(event.request.url);

    // 3. ✨ Ajuste pontual: Ignora rotas dinâmicas, painéis, dashboards e requisições do sistema operacional/desenvolvimento
    if (
        url.pathname.includes('/painel/') ||
        url.pathname.includes('/dashboard') ||
        url.pathname.includes('/detalhe_empresa') ||
        url.pathname.includes('__debugger__')
    ) {
        return; // Deixa o navegador lidar diretamente com a rede sem interceptação do SW
    }

    event.respondWith(
        fetch(event.request)
            .then((response) => response)
            .catch((err) => {
                // Se a requisição foi abortada pelo próprio navegador (ex: trocou de página/F5),
                // não força uma resposta 503.
                if (err.name === 'AbortError') {
                    return Promise.reject(err);
                }

                // Tenta buscar do cache se a rede realmente falhou
                return caches.match(event.request).then((cachedResponse) => {
                    if (cachedResponse) {
                        return cachedResponse;
                    }

                    // Se for uma navegação de página (HTML) e falhar totalmente,
                    // é melhor deixar o navegador tratar do que injetar um 503 de texto puro
                    if (event.request.mode === 'navigate') {
                        console.warn('Falha na navegação para:', event.request.url);
                        return Promise.reject(err);
                    }

                    console.log('Modo offline ou lentidão local detectada para:', event.request.url);
                    return new Response('Conexão instável detectada.', {
                        status: 503,
                        statusText: 'Service Unavailable',
                        headers: new Headers({ 'Content-Type': 'text/plain; charset=utf-8' })
                    });
                });
            })
    );
});