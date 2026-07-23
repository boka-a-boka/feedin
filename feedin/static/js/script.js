// =========================================================================
// 1. GESTÃO DE TEMA (DARK/LIGHT MODE)
// =========================================================================
const toggleButton = document.getElementById('darkModeToggle');

const setTheme = (theme) => {
    document.documentElement.setAttribute('data-bs-theme', theme);
    localStorage.setItem('theme', theme);
};

// Inicialização imediata para evitar "flash" de cor branca
const savedTheme = localStorage.getItem('theme') ||
                   (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
setTheme(savedTheme);

if (toggleButton) {
    toggleButton.addEventListener('click', () => {
        const currentTheme = document.documentElement.getAttribute('data-bs-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        setTheme(newTheme);
        toggleButton.textContent = newTheme === 'dark' ? 'Modo Claro' : 'Modo Escuro';
    });
}

// =========================================================================
// 2. FUNÇÃO AUXILIAR DE FETCH (PROTEÇÃO DE SESSÃO)
// =========================================================================
async function fetchProtegido(url, options = {}) {
    try {
        const response = await fetch(url, options);

        // Se o servidor redirecionar ou der erro de autorização, recarrega a página
        if (response.status === 401 || response.redirected) {
            console.warn("Sessão expirada. Redirecionando...");
            window.location.reload();
            return null;
        }
        return response;
    } catch (error) {
        console.error('Erro na requisição:', error);
        throw error;
    }
}

// =========================================================================
// 3. TROCA DE ABAS ASSÍNCRONA
// =========================================================================
function trocarAba(url) {
    const destino = document.getElementById('area-conteudo');
    if (!destino) return;

    fetchProtegido(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(response => response ? response.text() : '')
        .then(html => {
            if (html) destino.innerHTML = html;
        });
}

// =========================================================================
// 4. AUTOCOMPLETE E BUSCA DE LOCAIS (DOM CONTENT LOADED)
// =========================================================================
document.addEventListener('DOMContentLoaded', function() {
    const inputNome = document.getElementById('inputNomeLocal');
    const listaSugestoes = document.getElementById('listaSugestoes');
    const hiddenId = document.getElementById('hiddenLocalId');
    const camposExtra = document.getElementById('camposEnderecoExtra');
    const secaoCurtidas = document.getElementById('secao-curtidas');
    const nomeSpan = document.getElementById('nome-local-selecionado');

    if (inputNome) {
        inputNome.addEventListener('input', function() {
            const busca = this.value;
            if (hiddenId) hiddenId.value = ''; // Reseta o ID se o usuário voltar a digitar

            if (busca.length < 2) {
                if (listaSugestoes) listaSugestoes.classList.add('d-none');
                return;
            }

            fetch(`/buscar_locais?q=${encodeURIComponent(busca)}`)
                .then(res => res.json())
                .then(data => {
                    if (!listaSugestoes) return;
                    listaSugestoes.innerHTML = '';

                    if (data.length > 0) {
                        listaSugestoes.classList.remove('d-none');
                        data.forEach(local => {
                            const item = document.createElement('button');
                            item.type = 'button';
                            item.className = 'list-group-item list-group-item-action border-0';
                            item.innerHTML = `
                                <div class="d-flex justify-content-between align-items-center">
                                    <div>
                                        <strong class="text-dark">${local.nome}</strong><br>
                                        <small class="text-muted">${local.logradouro} - ${local.bairro}</small>
                                    </div>
                                    ${local.status === 'historico' ? '<span class="badge bg-secondary">Histórico</span>' : ''}
                                </div>`;

                            item.onclick = () => {
                                inputNome.value = local.nome;
                                if (hiddenId) hiddenId.value = local.id;
                                listaSugestoes.classList.add('d-none');
                                if (camposExtra) camposExtra.classList.add('d-none');

                                if (nomeSpan) nomeSpan.textContent = local.nome;
                                if (secaoCurtidas) secaoCurtidas.classList.remove('d-none');
                            };
                            listaSugestoes.appendChild(item);
                        });
                    } else {
                        // Opção de "Novo Local"
                        const novo = document.createElement('button');
                        novo.type = 'button';
                        novo.className = 'list-group-item list-group-item-action text-primary fw-bold';
                        novo.innerHTML = `<i class="bi bi-plus-circle me-2"></i> "${busca}" não encontrado. Cadastrar novo?`;
                        novo.onclick = () => {
                            inputNome.value = busca;
                            if (hiddenId) hiddenId.value = '';
                            listaSugestoes.classList.add('d-none');
                            if (camposExtra) camposExtra.classList.remove('d-none');
                            if (nomeSpan) nomeSpan.textContent = busca;
                            if (secaoCurtidas) secaoCurtidas.classList.remove('d-none');
                        };
                        listaSugestoes.appendChild(novo);
                        listaSugestoes.classList.remove('d-none');
                    }
                });
        });
    }

    // --- CONFIGURAÇÃO DO PREVIEW DE FOTO DE PERFIL ---
    const inputFotoPerfil = document.getElementById('foto-perfil-input');
    const fotoPreviewPerfil = document.getElementById('foto-preview');

    if (inputFotoPerfil && fotoPreviewPerfil) {
        inputFotoPerfil.addEventListener('change', function() {
            const file = this.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    fotoPreviewPerfil.src = e.target.result;
                };
                reader.readAsDataURL(file);
            }
        });
    }

    // --- CONFIGURAÇÃO DO SEGUNDO INPUT DE FOTO (COMPATIBILIDADE) ---
    const inputFotoGeral = document.getElementById('input-foto');
    const btnUpload = document.getElementById('btn-upload-foto');
    const nomeArquivo = document.getElementById('nome-arquivo');

    if (inputFotoGeral) {
        inputFotoGeral.addEventListener('change', function(event) {
            const file = event.target.files[0];
            const preview = document.getElementById('foto-preview');

            if (file && preview) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    preview.src = e.target.result;
                    if (btnUpload) btnUpload.classList.remove('d-none');
                    if (nomeArquivo) nomeArquivo.textContent = file.name;
                };
                reader.readAsDataURL(file);
            }
        });
    }
});

// =========================================================================
// 5. FUNÇÕES GLOBAIS DE ACIONAMENTO MANUAL
// =========================================================================
function selecionarLocal(nome) {
    const spanNome = document.getElementById('nome-local-selecionado');
    if (spanNome) spanNome.textContent = nome;
    document.getElementById('camposEnderecoExtra')?.classList.add('d-none');
    document.getElementById('secao-curtidas')?.classList.remove('d-none');
}

function abrirCadastroManual(termo) {
    const spanNome = document.getElementById('nome-local-selecionado');
    if (spanNome) spanNome.textContent = termo;

    document.getElementById('listaSugestoes')?.classList.add('d-none');
    document.getElementById('camposEnderecoExtra')?.classList.remove('d-none');
    document.getElementById('secao-curtidas')?.classList.remove('d-none');
    document.getElementById('regLogradouro')?.focus();

    const btnSubmit = document.querySelector('#formGrupoSocial button[type="submit"]');
    if (btnSubmit) {
        btnSubmit.className = 'btn btn-success w-100 py-2 fw-bold shadow-sm';
        btnSubmit.innerHTML = '<i class="bi bi-cloud-upload me-1"></i> Sugerir Local e Salvar';
    }
}

// =========================================================================
// 6. AUTENTICAÇÃO E CADASTRO BIOMÉTRICO (WEBAUTHN)
// =========================================================================
async function iniciarCadastroBiometrico(email, senha) {
    const resposta = await fetch('/ativar-biometria', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, senha })
    });

    if (!resposta.ok) {
        alert("Falha na validação dos dados.");
        return;
    }

    const dados = await resposta.json();
    const options = dados.options;

    options.challenge = Uint8Array.from(atob(options.challenge), c => c.charCodeAt(0));
    options.user.id = Uint8Array.from(atob(options.user.id), c => c.charCodeAt(0));

    try {
        const credential = await navigator.credentials.create({ publicKey: options });

        const dadosParaSalvar = {
            id: credential.id,
            rawId: btoa(String.fromCharCode.apply(null, new Uint8Array(credential.rawId))),
            type: credential.type,
            response: {
                attestationObject: btoa(String.fromCharCode.apply(null, new Uint8Array(credential.response.attestationObject))),
                clientDataJSON: btoa(String.fromCharCode.apply(null, new Uint8Array(credential.response.clientDataJSON)))
            }
        };

        const salvarResposta = await fetch('/salvar-biometria', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(dadosParaSalvar)
        });

        if (salvarResposta.ok) {
            alert("Biometria ativada com sucesso! Próximos logins serão diretos.");
            window.location.href = '/dashboard';
        }

    } catch (err) {
        console.error("O usuário cancelou ou o dispositivo não suporta:", err);
        alert("O processo de biometria foi cancelado ou não é suportado neste navegador.");
    }
}

// =========================================================================
// 7. INTEGRAÇÃO DE CEP AUTOMÁTICO (MÓDULO CORE / EMPRESAS)
// =========================================================================
document.addEventListener('blur', function (event) {
    if (event.target && event.target.id === 'cep') {
        const inputCep = event.target;
        let cep = inputCep.value.replace(/\D/g, '');

        if (cep.length === 8) {
            const campos = {
                logradouro: document.getElementById('logradouro'),
                bairro: document.getElementById('bairro'),
                cidade: document.getElementById('cidade'),
                estado: document.getElementById('estado'),
                numero: document.getElementById('numero')
            };

            const toggleCampos = (status) => {
                ['logradouro', 'bairro', 'cidade', 'estado'].forEach(id => {
                    if (campos[id]) campos[id].disabled = status;
                });
            };

            toggleCampos(true);
            if (campos.logradouro) campos.logradouro.value = 'Buscando endereço...';

            fetch(`https://viacep.com.br/ws/${cep}/json/`)
                .then(response => {
                    if (!response.ok) throw new Error('Falha na rede ao buscar CEP.');
                    return response.json();
                })
                .then(data => {
                    if (!data.erro) {
                        if (campos.logradouro) campos.logradouro.value = data.logradouro;
                        if (campos.bairro) campos.bairro.value = data.bairro;
                        if (campos.cidade) campos.cidade.value = data.localidade;
                        if (campos.estado) campos.estado.value = data.uf;

                        if (campos.numero) campos.numero.focus();
                    } else {
                        alert('CEP não localizado no banco de dados postal. Por favor, preencha manualmente.');
                        limparCamposEndereco(campos);
                    }
                })
                .catch(error => {
                    console.error('Erro na integração de CEP:', error);
                    alert('Não foi possível conectar ao serviço de busca de CEP. Insira o endereço manualmente.');
                    limparCamposEndereco(campos);
                })
                .finally(() => {
                    toggleCampos(false);
                });
        }
    }
}, true);

function limparCamposEndereco(campos) {
    ['logradouro', 'bairro', 'cidade', 'estado'].forEach(id => {
        if (campos[id]) campos[id].value = '';
    });
}

/**
 ==========================================================================================
 📌 SCRIPT CORE: GATILHO DE MUTAÇÃO VISUAL E PERSISTÊNCIA DO CLAIM
 ==========================================================================================
 Controla o envio do primeiro documento. Assim que o back-end responde com sucesso,
 ele sinaliza ao usuário que o status do Claim mudou e que o vínculo territorial foi feito.
 ==========================================================================================
 */

function enviarPrimeiroDocumento(event) {
    event.preventDefault();

    const form = event.target;
    const btnSubmit = document.getElementById('btnAtivar');
    const fileInput = document.getElementById('file_input');

    if (fileInput.files.length === 0) {
        alert("❌ Por favor, selecione um arquivo válido para prosseguir.");
        return;
    }

    // Travamento do botão para evitar cliques duplicados (Double-Click Protection)
    btnSubmit.disabled = true;
    btnSubmit.innerHTML = `<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Processando Ativação...`;

    const formData = new FormData(form);

    // Envia os dados para o endpoint que processará a mutação de status e gerará o Vínculo Core
    fetch('/empresa/api/ativar-claim', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (!response.ok) throw new Error('Ocorreu um erro no servidor de homologação.');
        return response.json();
    })
    .then(data => {
        if (data.status === 'success') {
            alert("🚀 Sensacional! Primeiro documento recebido. Seu Claim mudou para 'em_andamento_com_dados' e seu perfil foi vinculado ao local.");
            // Recarrega a página para liberar as próximas abas com o novo estado do banco
            window.location.reload();
        } else {
            throw new Error(data.message);
        }
    })
    .catch(error => {
        alert(`❌ Falha na ativação: ${error.message}`);
        btnSubmit.disabled = false;
        btnSubmit.innerHTML = `<i class="bi bi-cloud-arrow-up-fill me-1"></i> Concluir Ativação e Vincular`;
    });
}