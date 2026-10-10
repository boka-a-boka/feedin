/**
 * Gerenciador de Fechamento de Comanda - PDV FeedIn
 */
const PDVFechamento = {
    // Estado da comanda em edição
    estado: {
        cobrancaId: null,
        valorBruto: 0.0,
        valorDesconto: 0.0,
        valorAcrescimo: 0.0,
        valorLiquido: 0.0,
        valorPago: 0.0,
        troco: 0.0
    },

    /**
     * Carrega a comanda e abre o modal de fechamento
     */
    async abrirModalFechamento(cobrancaId) {
        try {
            const response = await fetch(`/pdv/api/comanda/${cobrancaId}/itens`);
            const data = await response.json();

            if (!response.ok || data.status !== 'sucesso') {
                alert(data.error || 'Erro ao carregar dados da comanda.');
                return;
            }

            this.estado.cobrancaId = cobrancaId;
            this.estado.valorBruto = parseFloat(data.valor_bruto || 0);
            this.estado.valorDesconto = parseFloat(data.valor_desconto || 0);
            this.estado.valorAcrescimo = parseFloat(data.valor_acrescimo || 0);

            // Preenche os inputs da interface
            document.getElementById('inputCobrancaId').value = cobrancaId;
            document.getElementById('inputValorDesconto').value = this.estado.valorDesconto.toFixed(2);
            document.getElementById('inputValorAcrescimo').value = this.estado.valorAcrescimo.toFixed(2);

            this.recalcularTotais();

            // Sugere o valor líquido como valor pago inicial
            document.getElementById('inputValorPago').value = this.estado.valorLiquido.toFixed(2);
            this.atualizarTroco();

            // Abre o modal de fechamento
            $('#modalFecharComanda').modal('show');
        } catch (error) {
            console.error('Erro ao abrir fechamento:', error);
            alert('Falha na comunicação com o servidor.');
        }
    },

    /**
     * Recalcula valor líquido com base nos descontos e acréscimos
     */
    recalcularTotais() {
        const desc = parseFloat(document.getElementById('inputValorDesconto').value) || 0;
        const acres = parseFloat(document.getElementById('inputValorAcrescimo').value) || 0;

        this.estado.valorDesconto = desc;
        this.estado.valorAcrescimo = acres;
        this.estado.valorLiquido = Math.max(0, this.estado.valorBruto + acres - desc);

        // Atualiza elementos DOM de exibição
        document.getElementById('lblValorBruto').innerText = `R$ ${this.estado.valorBruto.toFixed(2)}`;
        document.getElementById('lblValorLiquido').innerText = `R$ ${this.estado.valorLiquido.toFixed(2)}`;

        this.atualizarTroco();
    },

    /**
     * Calcula o troco quando o pagamento é em dinheiro ou superior ao total
     */
    atualizarTroco() {
        const valorPago = parseFloat(document.getElementById('inputValorPago').value) || 0;
        this.estado.valorPago = valorPago;
        this.estado.troco = Math.max(0, valorPago - this.estado.valorLiquido);

        const elTroco = document.getElementById('lblValorTroco');
        if (elTroco) {
            elTroco.innerText = `R$ ${this.estado.troco.toFixed(2)}`;
        }
    },

    /**
     * Envia o fechamento para a API
     */
    async confirmarFechamento() {
        if (!this.estado.cobrancaId) {
            alert('Nenhuma comanda selecionada.');
            return;
        }

        if (this.estado.valorPago < this.estado.valorLiquido) {
            alert(`O valor pago (R$ ${this.estado.valorPago.toFixed(2)}) é menor que o valor total (R$ ${this.estado.valorLiquido.toFixed(2)}).`);
            return;
        }

        const payload = {
            cobranca_id: this.estado.cobrancaId,
            valor_desconto: this.estado.valorDesconto,
            valor_acrescimo: this.estado.valorAcrescimo,
            valor_pago: this.estado.valorPago,
            forma_pagamento: document.getElementById('selectFormaPagamento').value,
            observacao: document.getElementById('inputObservacaoPagamento')?.value || ''
        };

        try {
            const response = await fetch('/pdv/api/comanda/fechar', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const result = await response.json();

            if (!response.ok || !result.sucesso) {
                alert(result.error || 'Erro ao finalizar a comanda.');
                return;
            }

            $('#modalFecharComanda').modal('hide');

            // Renderiza e exibe o comprovante
            this.exibirComprovante(result.comanda);

        } catch (error) {
            console.error('Erro na requisição de fechamento:', error);
            alert('Erro de conexão ao processar o pagamento.');
        }
    },

    /**
     * Monta a estrutura visual do comprovante no modal de recibo
     */
    exibirComprovante(comanda) {
        const container = document.getElementById('conteudoComprovante');
        if (!container) return;

        container.innerHTML = `
            <div class="ticket-comprovante text-center p-3 style="font-family: monospace;">
                <h4>COMPROVANTE DE PAGAMENTO</h4>
                <p class="mb-1">Transação: <strong>${comanda.codigo_transacao}</strong></p>
                <p class="mb-3">Data: ${comanda.data_pagamento}</p>
                <hr>
                <div class="text-left mb-2">
                    <div class="d-flex justify-content-between">
                        <span>Valor Subtotal:</span>
                        <span>R$ ${comanda.valor_bruto.toFixed(2)}</span>
                    </div>
                    ${comanda.valor_desconto > 0 ? `
                    <div class="d-flex justify-content-between text-success">
                        <span>Desconto:</span>
                        <span>- R$ ${comanda.valor_desconto.toFixed(2)}</span>
                    </div>` : ''}
                    ${comanda.valor_acrescimo > 0 ? `
                    <div class="d-flex justify-content-between text-danger">
                        <span>Acréscimo:</span>
                        <span>+ R$ ${comanda.valor_acrescimo.toFixed(2)}</span>
                    </div>` : ''}
                    <div class="d-flex justify-content-between font-weight-bold mt-2 fs-5">
                        <span>Total Pago:</span>
                        <span>R$ ${comanda.valor_liquido.toFixed(2)}</span>
                    </div>
                    <hr>
                    <div class="d-flex justify-content-between">
                        <span>Forma de Pagamento:</span>
                        <span>${comanda.forma_pagamento}</span>
                    </div>
                    <div class="d-flex justify-content-between">
                        <span>Valor Recebido:</span>
                        <span>R$ ${comanda.valor_pago.toFixed(2)}</span>
                    </div>
                    ${comanda.troco > 0 ? `
                    <div class="d-flex justify-content-between font-weight-bold text-primary">
                        <span>Troco:</span>
                        <span>R$ ${comanda.troco.toFixed(2)}</span>
                    </div>` : ''}
                </div>
                <hr>
                <p class="small text-muted">Obrigado pela preferência!</p>
            </div>
        `;

        $('#modalComprovante').modal('show');
    },

    /**
     * Dispara a impressão do comprovante em impressoras térmicas/padrão
     */
    imprimirComprovante() {
        const conteudo = document.getElementById('conteudoComprovante').innerHTML;
        const janelaImpressao = window.open('', '', 'width=400,height=600');

        janelaImpressao.document.write(`
            <html>
                <head>
                    <title>Comprovante PDV</title>
                    <link rel="stylesheet" href="/static/css/bootstrap.min.css">
                    <style>
                        body { font-family: monospace; padding: 10px; width: 80mm; }
                        @page { margin: 0; }
                    </style>
                </head>
                <body>
                    ${conteudo}
                    <script>
                        window.onload = function() { window.print(); window.close(); }
                    </script>
                </body>
            </html>
        `);
        janelaImpressao.document.close();
    }
};

// Event Listeners dos inputs para recalculo dinamico
document.addEventListener('DOMContentLoaded', () => {
    const inputDesc = document.getElementById('inputValorDesconto');
    const inputAcres = document.getElementById('inputValorAcrescimo');
    const inputPago = document.getElementById('inputValorPago');

    if (inputDesc) inputDesc.addEventListener('input', () => PDVFechamento.recalcularTotais());
    if (inputAcres) inputAcres.addEventListener('input', () => PDVFechamento.recalcularTotais());
    if (inputPago) inputPago.addEventListener('input', () => PDVFechamento.atualizarTroco());
});