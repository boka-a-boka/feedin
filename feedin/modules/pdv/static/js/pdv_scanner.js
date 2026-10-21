// =========================================================================
// MOTOR GLOBAL DE LEITOR DE CÓDIGO DE BARRAS E CÂMERA (MÓDULO PDV)
// =========================================================================
let html5QrCodeEstoque = null;
let targetInputIdGlobal = null;

// AVISO SONORO (BEEP) NATIVO
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

// FUNÇÃO GLOBAL DE ABERTURA DE CÂMERA (Compatível com qualquer tela)
window.abrirLeitorCameraUniversal = function(targetInputId) {
    targetInputIdGlobal = targetInputId;
    const modalEl = document.getElementById('modalLeitorCamera');
    if (!modalEl) {
        console.error("Modal com ID #modalLeitorCamera não encontrado na página.");
        return;
    }

    const modal = new bootstrap.Modal(modalEl);
    modal.show();

    if (typeof Html5Qrcode === 'undefined') {
        alert("Biblioteca de câmera não carregada.");
        return;
    }

    // Garante que a div #reader existe
    if (!document.getElementById('reader')) {
        console.error("Elemento com ID #reader não encontrado no modal.");
        return;
    }

    html5QrCodeEstoque = new Html5Qrcode("reader");
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
        formatsToSupport: [
            Html5QrcodeSupportedFormats.EAN_13,
            Html5QrcodeSupportedFormats.EAN_8,
            Html5QrcodeSupportedFormats.CODE_128,
            Html5QrcodeSupportedFormats.UPC_A,
            Html5QrcodeSupportedFormats.UPC_E,
            Html5QrcodeSupportedFormats.CODE_39,
            Html5QrcodeSupportedFormats.CODE_93,
            Html5QrcodeSupportedFormats.ITF,      // Usado em caixas/fardos fechados (Interleaved 2 of 5)
            Html5QrcodeSupportedFormats.QR_CODE  // Suporte a QR Code
        ],
        experimentalFeatures: {
            useBarCodeDetectorIfSupported: true
        }
    };

    html5QrCodeEstoque.start(
        { facingMode: "environment" },
        config,
        (decodedText) => {
            if (typeof window.tocarBip === 'function') window.tocarBip();

            if (targetInputIdGlobal) {
                const inputTarget = document.getElementById(targetInputIdGlobal);
                if (inputTarget) {
                    inputTarget.value = decodedText;
                    inputTarget.dispatchEvent(new Event('input'));
                    inputTarget.dispatchEvent(new Event('change'));
                }
            }
            window.fecharLeitorCameraUniversal();
        }
    ).catch(err => {
        console.error("Erro ao iniciar câmera:", err);
        window.fecharLeitorCameraUniversal();
    });
};

window.fecharLeitorCameraUniversal = function() {
    if (html5QrCodeEstoque) {
        html5QrCodeEstoque.stop().then(() => {
            html5QrCodeEstoque.clear();
            html5QrCodeEstoque = null;
        }).catch(() => {
            html5QrCodeEstoque = null;
        });
    }
    const modalEl = document.getElementById('modalLeitorCamera');
    if (modalEl) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }
};