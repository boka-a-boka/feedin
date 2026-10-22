// =========================================================================
// MOTOR GLOBAL ROBUSTO DE CÂMERA E LEITOR DE CÓDIGO DE BARRAS (PDV)
// =========================================================================
let html5QrCodeGlobal = null;
let targetInputIdGlobal = null;

// Audio Beep Nativo
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

// LIBERAÇÃO FORÇADA DE HARDWARE DA CÂMERA (iOS/Android)
async function liberarHardwareCamera() {
    if (html5QrCodeGlobal) {
        try {
            await html5QrCodeGlobal.stop();
            html5QrCodeGlobal.clear();
        } catch (e) {
            // Se já estivesse parado, ignora
        }
        html5QrCodeGlobal = null;
    }

    // Mata qualquer rastro de stream preso no navegador móvel
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ video: true }).catch(() => null);
            if (stream) {
                stream.getTracks().forEach(track => track.stop());
            }
        } catch (e) {
            // Ignora
        }
    }
}

// ABERTURA UNIVERSAL
window.abrirLeitorCameraUniversal = async function(targetInputId) {
    targetInputIdGlobal = targetInputId;

    // 1. Limpa qualquer processo residual de câmera no celular
    await liberarHardwareCamera();

    const modalEl = document.getElementById('modalLeitorCamera');
    if (!modalEl) {
        alert("Erro: Elemento #modalLeitorCamera não encontrado.");
        return;
    }

    const modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    // 2. Garante que a câmera SÓ INICIA quando o modal estiver 100% visível
    const iniciarScannerAposModal = async function () {
        modalEl.removeEventListener('shown.bs.modal', iniciarScannerAposModal);

        if (typeof Html5Qrcode === 'undefined') {
            alert("Biblioteca Html5Qrcode não carregada.");
            return;
        }

        const containerReader = document.getElementById('reader');
        if (!containerReader) return;
        containerReader.innerHTML = ''; // Limpa div interna

        html5QrCodeGlobal = new Html5Qrcode("reader");

        const config = {
            fps: 15,
            qrbox: function(viewfinderWidth, viewfinderHeight) {
                const minDimension = Math.min(viewfinderWidth, viewfinderHeight);
                const size = Math.floor(minDimension * 0.75);
                return {
                    width: Math.max(size, 200),
                    height: Math.floor(Math.max(size, 200) * 0.5)
                };
            },
            formatsToSupport: [
                Html5QrcodeSupportedFormats.EAN_13,
                Html5QrcodeSupportedFormats.EAN_8,
                Html5QrcodeSupportedFormats.CODE_128,
                Html5QrcodeSupportedFormats.UPC_A,
                Html5QrcodeSupportedFormats.UPC_E,
                Html5QrcodeSupportedFormats.CODE_39
            ],
            experimentalFeatures: {
                useBarCodeDetectorIfSupported: true
            }
        };

        try {
            await html5QrCodeGlobal.start(
                { facingMode: "environment" },
                config,
                (decodedText) => {
                    if (typeof window.tocarBip === 'function') window.tocarBip();

                    if (targetInputIdGlobal) {
                        const inputTarget = document.getElementById(targetInputIdGlobal);
                        if (inputTarget) {
                            inputTarget.value = decodedText;
                            inputTarget.dispatchEvent(new Event('input', { bubbles: true }));
                            inputTarget.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                    window.fecharLeitorCameraUniversal();
                }
            );
        } catch (err) {
            console.error("Erro ao iniciar câmera:", err);
            alert("Não foi possível acessar a câmera. Verifique as permissões do navegador.");
            window.fecharLeitorCameraUniversal();
        }
    };

    modalEl.addEventListener('shown.bs.modal', iniciarScannerAposModal);
    modal.show();
};

// FECHAMENTO UNIVERSAL
window.fecharLeitorCameraUniversal = async function() {
    await liberarHardwareCamera();

    const modalEl = document.getElementById('modalLeitorCamera');
    if (modalEl) {
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();
    }
};