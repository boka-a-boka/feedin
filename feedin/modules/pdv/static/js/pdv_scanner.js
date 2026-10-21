// =========================================================================
// MOTOR GLOBAL DE LEITOR DE CÓDIGO DE BARRAS E CÂMERA (MÓDULO PDV)
// =========================================================================
let html5QrCodeEstoque = null;
let timerDebounceEstoque = null;

// 1. AVISO SONORO (BEEP) NATIVO
window.tocarBip = function() {
    try {
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = 1200;
        gain.gain.value = 0.1;
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start();
        osc.stop(audioCtx.currentTime + 0.12);
    } catch (e) {
        // Áudio opcional
    }
};

// 2. INICIALIZAÇÃO DE EVENTOS DO MODAL E INPUT
document.addEventListener('DOMContentLoaded', function() {
    const modalItemColab = document.getElementById('modalAdicionarItemColaborador');
    const inputBarras = document.getElementById('inputBarrasEstoque');

    if (modalItemColab) {
        // Foco automático ao abrir o modal
        modalItemColab.addEventListener('shown.bs.modal', function () {
            if (inputBarras) {
                inputBarras.focus();
                inputBarras.select();
            }
        });

        // Encerramento seguro da câmera ao fechar o modal
        modalItemColab.addEventListener('hidden.bs.modal', function () {
            window.pararLeitorCamera();
        });
    }

    // Escuta a tecla Enter disparada por leitores USB físicos
    if (inputBarras) {
        inputBarras.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                clearTimeout(timerDebounceEstoque);
                const valor = this.value.trim();
                if (valor && typeof consultarProdutoEstoque === 'function') {
                    window.tocarBip();
                    consultarProdutoEstoque(valor, true);
                }
            }
        });
    }
});

// 3. CONTROLE DE DEBOUNCE PARA DIGITAÇÃO RÁPIDA
window.aoDigitarCodigoEstoque = function(valor) {
    clearTimeout(timerDebounceEstoque);
    timerDebounceEstoque = setTimeout(() => {
        if (typeof consultarProdutoEstoque === 'function') {
            consultarProdutoEstoque(valor, false);
        }
    }, 300);
};

// 4. LÓGICA DE CÂMERA INLINE COM RECORTES PARA EAN-13
window.alternarLeitorCamera = function() {
    const container = document.getElementById('container-scanner-camera');
    if (!container) return;

    if (!container.classList.contains('d-none')) {
        window.pararLeitorCamera();
        return;
    }

    if (typeof Html5Qrcode === 'undefined') {
        alert("A biblioteca leitora de código de barras não está carregada.");
        return;
    }

    container.classList.remove('d-none');

    try {
        html5QrCodeEstoque = new Html5Qrcode("reader-camera-estoque");

        const config = {
            fps: 15,
            qrbox: function(viewfinderWidth, viewfinderHeight) {
                const minEdgePercentage = 0.75;
                const minDimension = Math.min(viewfinderWidth, viewfinderHeight);
                const qrboxSize = Math.floor(minDimension * minEdgePercentage);
                return {
                    width: Math.max(qrboxSize, 220),
                    height: Math.floor(Math.max(qrboxSize, 220) * 0.5)
                };
            },
            experimentalFeatures: {
                useBarCodeDetectorIfSupported: true
            }
        };

        html5QrCodeEstoque.start(
            { facingMode: "environment" },
            config,
            onScanSuccessEstoque
        ).catch(err => {
            console.error("Erro ao iniciar câmera:", err);
            window.pararLeitorCamera();
        });

    } catch (e) {
        console.error("Exceção na inicialização da câmera:", e);
        window.pararLeitorCamera();
    }
};

function onScanSuccessEstoque(decodedText) {
    window.tocarBip();

    const inputBarras = document.getElementById('inputBarrasEstoque');
    if (inputBarras) {
        inputBarras.value = decodedText;
        if (typeof consultarProdutoEstoque === 'function') {
            consultarProdutoEstoque(decodedText, true);
        }
    }

    window.pararLeitorCamera();
}

window.pararLeitorCamera = function() {
    const container = document.getElementById('container-scanner-camera');

    if (html5QrCodeEstoque) {
        html5QrCodeEstoque.stop().then(() => {
            html5QrCodeEstoque.clear();
            html5QrCodeEstoque = null;
            if (container) container.classList.add('d-none');
        }).catch(() => {
            html5QrCodeEstoque = null;
            if (container) container.classList.add('d-none');
        });
    } else {
        if (container) container.classList.add('d-none');
    }
};