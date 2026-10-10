/* ==================================================================================
 * 📱 MÓDULO DE AUTO COMPRA PELO CELULAR (LEITOR MOBILE + COMANDA)
 * ================================================================================== */

let html5QrCodeScanner = null;
let produtoAtual = null;

// Exposição explícita no escopo global para garantir a execução do onclick no HTML
window.abrirModalAutoCompra = function() {
    const modalEl = document.getElementById('modalAutoCompraCliente') || document.getElementById('modalSelfServiceCompra');
    if (!modalEl) {
        console.error("Modal de Auto Compra não foi encontrado no DOM.");
        return;
    }

    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
        let instance = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
        instance.show();
    } else if (typeof $!== 'undefined' &&$.fn.modal) {
        $(modalEl).modal('show');
    } else {
        console.error("Bootstrap JS não carregado no escopo global.");
    }
};

window.alternarCameraMobile = function() {
    const container = document.getElementById('areaScannerCamera');
    if (!container) return;

    if (container.classList.contains('d-none')) {
        container.classList.remove('d-none');
        window.iniciarCameraMobile();
    } else {
        window.pararCameraMobile();
    }
};

window.iniciarCameraMobile = function() {
    if (html5QrCodeScanner) {
        window.pararCameraMobile();
    }

    html5QrCodeScanner = new Html5Qrcode("readerCameraMobile");
    const config = { fps: 10, qrbox: { width: 220, height: 150 } };

    html5QrCodeScanner.start(
        { facingMode: "environment" },
        config,
        (decodedText) => {
            window.pararCameraMobile();
            window.consultarProdutoPorCodigo(decodedText);
        },
        () => {}
    ).catch(err => {
        console.warn("Não foi possível acessar a câmera do celular:", err);
        alert("Permissão de câmera necessária para escanear o produto.");
        window.pararCameraMobile();
    });
};

window.pararCameraMobile = function() {
    const container = document.getElementById('areaScannerCamera');
    if (container) container.classList.add('d-none');

    if (html5QrCodeScanner) {
        html5QrCodeScanner.stop().then(() => {
            html5QrCodeScanner.clear();
            html5QrCodeScanner = null;
        }).catch(() => {
            html5QrCodeScanner = null;
        });
    }
};

window.consultarProdutoPorCodigo = function(codigo) {
    const empresaId = window.EMPRESA_ATIVA_ID || '';
    if (!empresaId) {
        alert("Empresa não identificada para a consulta.");
        return;
    }

    fetch(`/agenda/estoque/api/produto/buscar-barras/${codigo}?empresa_id=${empresaId}`)
        .then(res => res.json())
        .then(data => {
            if (data.success && data.produto) {
                window.selecionarProdutoAutoCompra(data.produto);
            } else {
                alert("Produto não localizado no estoque.");
            }
        })
        .catch(err => console.error("Erro ao buscar produto:", err));
};

/**
 * Renderiza o produto selecionado no Card e ativa o botão de confirmação
 */
window.selecionarProdutoAutoCompra = function(prod) {
    const hdnId = document.getElementById('hdnProdutoIdSelecionado');
    const lblNome = document.getElementById('lblNomeProduto');
    const lblEstoque = document.getElementById('lblEstoqueDisponivel');
    const lblPreco = document.getElementById('lblPrecoProduto');
    const boxResult = document.getElementById('boxProdutoSelecionado');
    const btnConfirmar = document.getElementById('btnConfirmarAutoCompra');
    const dropdown = document.getElementById('dropdownResultadoMobile');

    if (hdnId) hdnId.value = prod.id;
    if (lblNome) lblNome.innerText = prod.nome;
    if (lblEstoque) lblEstoque.innerText = `Estoque: ${prod.estoque_atual} un`;
    if (lblPreco) {
        const v = parseFloat(prod.preco_venda || 0);
        lblPreco.innerText = v.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
    }

    const inputQtd = document.getElementById('numQtdAutoCompra');
    if (inputQtd) inputQtd.value = 1;

    if (boxResult) boxResult.classList.remove('d-none');
    if (btnConfirmar) btnConfirmar.disabled = false;
    if (dropdown) dropdown.classList.add('d-none');
};

window.alterarQtdMobile = function(delta) {
    const input = document.getElementById('numQtdAutoCompra');
    if (!input) return;

    let v = parseInt(input.value) + delta;
    if (v < 1) v = 1;
    input.value = v;
};

window.confirmarPedidoAutoCompra = function() {
    const hdnProdutoId = document.getElementById('hdnProdutoIdSelecionado');
    const inputQtd = document.getElementById('numQtdAutoCompra');

    if (!hdnProdutoId || !hdnProdutoId.value) {
        alert("Por favor, selecione um produto antes de confirmar.");
        return;
    }

    const produtoId = parseInt(hdnProdutoId.value, 10);
    const quantidade = parseInt(inputQtd ? inputQtd.value : 1, 10);

    let empresaId = window.EMPRESA_ATIVA_ID;
    if (!empresaId || empresaId === 'None' || empresaId === 'null') {
        const hdnEmpresa = document.getElementById('hdnEmpresaAtivaId');
        if (hdnEmpresa) empresaId = hdnEmpresa.value;
    }

    const empresaIdInt = parseInt(empresaId, 10);

    if (isNaN(empresaIdInt) || isNaN(produtoId)) {
        alert("Identificador do produto ou da empresa é inválido.");
        return;
    }

    const btnConfirmar = document.getElementById('btnConfirmarAutoCompra');
    if (btnConfirmar) {
        btnConfirmar.disabled = true;
        btnConfirmar.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span> Adicionando...`;
    }

    // 🟢 Tenta resgatar o CSRF Token da página (se existir)
    let csrfToken = '';
    const metaCsrf = document.querySelector('meta[name="csrf-token"]');
    if (metaCsrf) {
        csrfToken = metaCsrf.getAttribute('content');
    } else {
        const inputCsrf = document.querySelector('input[name="csrf_token"]');
        if (inputCsrf) csrfToken = inputCsrf.value;
    }

    const headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    };

    if (csrfToken) {
        headers['X-CSRFToken'] = csrfToken;
    }

    fetch('/agenda/api/cliente/auto-compra', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
            empresa_id: empresaIdInt,
            itens: [{ produto_id: produtoId, quantidade: quantidade }]
        })
    })
    .then(async response => {
        const data = await response.json().catch(() => null);

        if (!response.ok) {
            const msg = (data && data.message) ? data.message : `Falha no servidor (Status ${response.status})`;
            throw new Error(msg);
        }
        return data;
    })
    .then(data => {
        if (data && data.success) {
            if (typeof window.pararCameraMobile === 'function') {
                window.pararCameraMobile();
            }
            alert("✅ Produto adicionado à comanda!");
            window.location.reload();
        } else {
            alert((data && data.message) ? data.message : "Erro ao registrar produto.");
            if (btnConfirmar) {
                btnConfirmar.disabled = false;
                btnConfirmar.innerHTML = `<i class="bi bi-cart-check me-1"></i> Adicionar à Comanda`;
            }
        }
    })
    .catch(err => {
        console.error("Erro na auto-compra:", err);
        alert(err.message || "Erro de comunicação com o servidor.");
        if (btnConfirmar) {
            btnConfirmar.disabled = false;
            btnConfirmar.innerHTML = `<i class="bi bi-cart-check me-1"></i> Adicionar à Comanda`;
        }
    });
};


/**
 * 🔍 Autocomplete em tempo real para Auto Compra no Celular
 */
window.buscarProdutoMobile = function(termo) {
    termo = termo ? termo.trim() : '';

    const inputBusca = document.getElementById('inputBuscaProdutoMobile');
    const containerResultados = document.getElementById('dropdownResultadoMobile');

    if (!containerResultados) return;

    if (termo.length < 2) {
        containerResultados.innerHTML = '';
        containerResultados.classList.add('d-none');
        return;
    }

    const empresaId = window.EMPRESA_ATIVA_ID || '';
    const urlBusca = `/agenda/estoque/api/produto/buscar-barras/${encodeURIComponent(termo)}${empresaId ? '?empresa_id=' + empresaId : ''}`;

    fetch(urlBusca)
        .then(response => {
            if (!response.ok) throw new Error('Erro na requisição');
            return response.json();
        })
        .then(data => {
            containerResultados.innerHTML = '';

            let produtos = Array.isArray(data) ? data : (data.produtos || (data.produto ? [data.produto] : []));

            if (!produtos || produtos.length === 0) {
                containerResultados.innerHTML = '<div class="p-2 text-muted small bg-white border rounded shadow-sm text-center">Nenhum produto encontrado</div>';
                containerResultados.classList.remove('d-none');
                return;
            }

            const listGroup = document.createElement('div');
            listGroup.className = 'list-group shadow-sm border rounded-3 bg-white overflow-hidden';

            produtos.forEach(prod => {
                const item = document.createElement('a');
                item.href = '#';
                item.className = 'list-group-item list-group-item-action d-flex justify-content-between align-items-center py-2 px-3 border-bottom-subtle';

                const vUnit = parseFloat(prod.preco_venda || prod.preco || prod.preco_unitario || prod.valor || 0);
                const precoFmt = vUnit.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });

                // 🟢 Fallbacks defensivos para resgate do Saldo de Estoque
                const saldoEstoque = prod.estoque_atual !== undefined ? prod.estoque_atual :
                                     (prod.saldo !== undefined ? prod.saldo :
                                     (prod.quantidade !== undefined ? prod.quantidade :
                                     (prod.quantidade_estoque !== undefined ? prod.quantidade_estoque :
                                     (prod.estoque !== undefined ? prod.estoque : 0))));

                item.innerHTML = `
                    <div class="text-truncate me-2">
                        <strong class="d-block text-dark small text-truncate">${prod.nome || prod.descricao}</strong>
                        <small class="text-muted d-block" style="font-size: 0.72rem;">Cód: ${prod.codigo_barras || prod.codigo || 'S/N'} | Est: ${saldoEstoque} un</small>
                    </div>
                    <span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill fw-bold flex-shrink-0">${precoFmt}</span>
                `;

                item.addEventListener('click', function(e) {
                    e.preventDefault();

                    if (inputBusca) {
                        inputBusca.value = prod.nome || prod.descricao;
                    }

                    window.selecionarProdutoAutoCompra({
                        id: prod.id,
                        nome: prod.nome || prod.descricao,
                        preco_venda: vUnit,
                        estoque_atual: saldoEstoque
                    });

                    containerResultados.innerHTML = '';
                    containerResultados.classList.add('d-none');
                });

                listGroup.appendChild(item);
            });

            containerResultados.appendChild(listGroup);
            containerResultados.classList.remove('d-none');
        })
        .catch(err => {
            console.error('Erro ao processar autocomplete:', err);
            containerResultados.classList.add('d-none');
        });
};

// Exemplo de como ajustar no seu auto_compra.js ao fechar o modal:
const fecharModalAutoCompra = () => {
    // 1. Tira o foco do elemento ativo atual (evita o aviso do aria-hidden)
    if (document.activeElement) {
        document.activeElement.blur();
    }

    // 2. Fecha o modal usando a API do Bootstrap
    const modalEl = document.getElementById('modalAutoCompraCliente');
    if (modalEl) {
        const modalInstance = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
        modalInstance.hide();
    }
};

document.addEventListener('DOMContentLoaded', function() {
    const modalAutoCompra = document.getElementById('modalAutoCompraCliente');

    if (modalAutoCompra) {
        // Intercepta o evento do Bootstrap antes do modal começar a fechar
        modalAutoCompra.addEventListener('hide.bs.modal', function () {
            const activeEl = document.activeElement;
            // Se o elemento em foco estiver dentro do modal, tira o foco (blur) imediatamente
            if (activeEl && modalAutoCompra.contains(activeEl)) {
                activeEl.blur();
            }
        });
    }
});