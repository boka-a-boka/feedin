from flask import Blueprint, url_for

agenda_bp = Blueprint(
    'agenda',
    __name__,
    template_folder='templates',
    static_folder='static',
    static_url_path='/agenda/static'  # Isolado para não colidir com o Core/outros módulos
)


# ============================================================
# FILTRO PARA LOGOS DE EMPRESAS (EseEmpresa.logomarca)
# ============================================================

@agenda_bp.app_template_filter('url_logo')
def formatar_url_logo(raw_logo):
    """
    Converte o caminho da logo da empresa em uma URL pública.
    Suporta URLs externas, caminhos relativos e previne erros de rota estática.
    """
    if not raw_logo or not isinstance(raw_logo, str):
        return None

    raw_logo = raw_logo.strip()
    if not raw_logo:
        return None

    # 1. URL externa (S3, CDN, Web)
    if raw_logo.startswith(('http://', 'https://', '//')):
        return raw_logo

    # 2. Normalização de barras
    raw_logo = raw_logo.replace('\\', '/').lstrip('/')

    # 3. Tratamento de caminhos e subpastas
    if 'uploads/' in raw_logo:
        caminho_relativo = raw_logo.split('uploads/', 1)[1].lstrip('/')
    elif 'empresas/' in raw_logo:
        caminho_relativo = 'empresas/' + raw_logo.split('empresas/', 1)[1].lstrip('/')
    else:
        caminho_relativo = f"logos/{raw_logo}"

    # Retorna sempre através da rota global /media/
    return f"/media/{caminho_relativo}"


# ============================================================
# HELPER GLOBAL DE RESOLUÇÃO DE MÍDIAS E AVATARES
# ============================================================

@agenda_bp.app_template_global('get_avatar_url')
def get_avatar_url(caminho_banco, pasta_subpasta=''):
    """
    Resolve o caminho de qualquer mídia/avatar para renderização segura no Jinja.
    Redireciona fotos de colaboradores para a rota estática do blueprint 'empresa'
    e demais mídias para a rota global /media/.
    """
    if not caminho_banco or not isinstance(caminho_banco, str):
        return None

    caminho_str = caminho_banco.strip()
    if not caminho_str or caminho_str.lower() in ['none', 'null', '']:
        return None

    # 1. URL externa ou caminho absoluto web
    if caminho_str.startswith(('http://', 'https://', '//')):
        return caminho_str

    # 2. Normalização de barras (Garante compatibilidade Windows/Linux)
    caminho_limpo = caminho_str.replace('\\', '/').lstrip('/')

    # 3. ROTEAMENTO ESPECIAL: Fotos de Colaboradores (Blueprint Empresa)
    # Exemplo no banco: 'uploads/empresas/2/colaboradores/foto.jpg' ou 'empresas/2/colaboradores/foto.jpg'
    if "colaboradores" in caminho_limpo.lower():
        if "uploads/" in caminho_limpo.lower():
            # Extrai tudo após 'uploads/' -> 'empresas/2/colaboradores/foto.jpg'
            partes = caminho_limpo.lower().split("uploads/", 1)
            caminho_relativo = partes[-1]
        else:
            caminho_relativo = caminho_limpo

        # Aponta para a rota estática nativa do blueprint 'empresa'
        # Gera a URL: /empresa/static/uploads/empresas/...
        return url_for('empresa.static', filename=f"uploads/{caminho_relativo}")

    # 4. ROTEAMENTO PADRÃO: Limpeza de prefixos para rotas globais /media/
    prefixos_para_remover = [
        'agenda/agenda/static/',
        'empresa/empresa/static/',
        'agenda/static/',
        'empresa/static/',
        'agenda/agenda/',
        'empresa/empresa/',
        'static/uploads/',
        'agenda/',
        'empresa/',
        'static/',
        'uploads/'
    ]

    removeu = True
    while removeu:
        removeu = False
        for prefixo in prefixos_para_remover:
            if caminho_limpo.startswith(prefixo):
                caminho_limpo = caminho_limpo[len(prefixo):]
                removeu = True

    # 5. Ajuste de subpasta opcional
    if pasta_subpasta:
        subpasta_clean = pasta_subpasta.strip('/')
        if not caminho_limpo.startswith(subpasta_clean + '/'):
            clean_path = f"{subpasta_clean}/{caminho_limpo}"
        else:
            clean_path = caminho_limpo
    else:
        clean_path = caminho_limpo

    # 6. Retorna a rota global /media/ para avatares comuns e perfis
    return f"/media/{clean_path.lstrip('/')}"


# ============================================================
# IMPORTAÇÕES (Mantidas ao final para evitar Circular Imports)
# ============================================================

from . import routes, models, forms