// --- 1. GESTÃO DE TEMA (DARK/LIGHT MODE) ---
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

// --- 2. FUNÇÃO AUXILIAR DE FETCH (PROTEÇÃO DE SESSÃO) ---
// Esta função centraliza as chamadas e verifica se a sessão caiu
async function fetchProtegido(url, options = {}) {
    try {
        const response = await fetch(url, options);

        // Se o servidor redirecionar ou der erro de autorização, recarrega a página
        if (response.status === 401 || response.redirected) {
            console.warn("Sessão expirada. Redirecionando...");
            window.location.reload();
            return;
        }
        return response;
    } catch (error) {
        console.error('Erro na requisição:', error);
        throw error;
    }
}

// --- 3. TROCA DE ABAS ASSÍNCRONA ---
function trocarAba(url) {
    const destino = document.getElementById('area-conteudo');
    if (!destino) return;

    fetchProtegido(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(response => response.text())
        .then(html => {
            if (html) destino.innerHTML = html;
        });
}

document.addEventListener('DOMContentLoaded', function() {
    const inputNome = document.getElementById('inputNomeLocal');
    const listaSugestoes = document.getElementById('listaSugestoes');
    const hiddenId = document.getElementById('hiddenLocalId');
    const camposExtra = document.getElementById('camposEnderecoExtra');
    const secaoCurtidas = document.getElementById('secao-curtidas');
    const nomeSpan = document.getElementById('nome-local-selecionado');

    inputNome.addEventListener('input', function() {
        const busca = this.value;
        hiddenId.value = ''; // Reseta o ID se o usuário voltar a digitar

        if (busca.length < 2) {
            listaSugestoes.classList.add('d-none');
            return;
        }

        fetch(`/buscar_locais?q=${encodeURIComponent(busca)}`)
            .then(res => res.json())
            .then(data => {
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
                            hiddenId.value = local.id; // CAPTURA O ID DO BANCO
                            listaSugestoes.classList.add('d-none');
                            camposExtra.classList.add('d-none'); // Esconde pois já temos os dados

                            // Mostra a seção de experiência
                            nomeSpan.textContent = local.nome;
                            secaoCurtidas.classList.remove('d-none');
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
                        hiddenId.value = ''; // ID vazio indica novo cadastro
                        listaSugestoes.classList.add('d-none');
                        camposExtra.classList.remove('d-none'); // Abre campos de endereço
                        nomeSpan.textContent = busca;
                        secaoCurtidas.classList.remove('d-none');
                    };
                    listaSugestoes.appendChild(novo);
                    listaSugestoes.classList.remove('d-none');
                }
            });
    });
});

function selecionarLocal(local) {
    inputNome.value = local.nome;
    listaSugestoes.classList.add('d-none');
    camposExtra.classList.add('d-none'); // Esconde campos de endereço pois já existe

    // Abre a seção de "O que você curte"
    nomeSelecionadoSpan.textContent = local.nome;
    secaoCurtidas.classList.remove('d-none');
}

function prepararNovoLocal(nome) {
    inputNome.value = nome;
    listaSugestoes.classList.add('d-none');

    // Mostra campos de endereço para o usuário preencher
    camposExtra.classList.remove('d-none');

    // Também mostra a seção de curtidas
    nomeSelecionadoSpan.textContent = nome;
    secaoCurtidas.classList.remove('d-none');
}

// --- 5. FUNÇÕES GLOBAIS ---

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

document.addEventListener('DOMContentLoaded', function() {
    console.log("=== INICIANDO CONFIGURAÇÃO DO PREVIEW ===");

    // Buscar o input de arquivo
    const inputFoto = document.getElementById('foto-perfil-input');
    const fotoPreview = document.getElementById('foto-preview');

    // Verificar se encontrou os elementos
    console.log("Input encontrado?", inputFoto);
    console.log("Preview encontrado?", fotoPreview);

    if (!inputFoto) {
        console.error("❌ Campo de arquivo NÃO encontrado! IDs disponíveis:");
        // Listar todos os inputs do formulário para debug
        document.querySelectorAll('input').forEach(input => {
            console.log("- Input ID:", input.id, "Type:", input.type);
        });
        return;
    }

    if (!fotoPreview) {
        console.error("❌ Elemento de preview NÃO encontrado!");
        return;
    }

    // Adicionar evento de change
    inputFoto.addEventListener('change', function(event) {
        console.log("✅ Evento 'change' disparado!");
        visualizarFoto(this);
    });

    console.log("✅ Configuração concluída! Aguardando seleção de arquivo...");
});

document.getElementById('input-foto').addEventListener('change', function(event) {
    const file = event.target.files[0];
    const preview = document.getElementById('foto-preview');
    const btnUpload = document.getElementById('btn-upload-foto');
    const nomeArquivo = document.getElementById('nome-arquivo');

    if (file) {
        const reader = new FileReader();

        reader.onload = function(e) {
            // Atualiza o src da imagem com o conteúdo do arquivo
            preview.src = e.target.result;
            // Mostra o botão de confirmação
            btnUpload.classList.remove('d-none');
            // Atualiza o nome do arquivo
            nomeArquivo.textContent = file.name;
        }

        reader.readAsDataURL(file);
    }
});

