import os
import random
from datetime import datetime, timezone
from PIL import Image
from flask import render_template, redirect, url_for, request, flash, current_app
from flask_login import login_required, current_user
from sqlalchemy.sql import expression as func

from feedin import database
from feedin.models import Local, Taxonomia
from feedin.routes import apenas_admin
from . import publicidade_bp
from .models import PublCampanha, PublClique


def _garantir_diretorio_slot(modulo_slug, nome_slot):
    """
    =============================================================================
    AUTOPROVISIONAMENTO DE DIRETÓRIOS DE MÍDIA
    =============================================================================
    Verifica se a estrutura física de diretórios para o módulo e slot específicos
    já existe no diretório estático. Caso contrário, cria a árvore completa
    automaticamente em tempo de execução.

    Caminho gerado:
        {UPLOAD_BASE_DIR}/{modulo_slug}/slots/{nome_slot}/

    Args:
        modulo_slug (str): O slug identificador do módulo (ex: 'agenda', 'geral').
        nome_slot (str): O nome do espaço de exibição (ex: 'dashboard_cliente').

    Returns:
        str: O caminho absoluto do diretório pronto para receber arquivos.
    =============================================================================
    """
    caminho_destino = os.path.join(
        publicidade_bp.UPLOAD_BASE_DIR,
        modulo_slug,
        'slots',
        nome_slot
    )
    os.makedirs(caminho_destino, exist_ok=True)
    return caminho_destino


def _salvar_imagem_publ(arquivo_foto, modulo_slug, nome_slot):
    """
    =============================================================================
    PROCESSAMENTO E CONVERSÃO DE IMAGENS DE PUBLICIDADE
    =============================================================================
    Recebe o arquivo de imagem enviado via formulário, valida, converte o formato
    para WebP otimizado, aplica um carimbo de data/hora para evitar colisões
    e o armazena na pasta correta do slot correspondente.

    Args:
        arquivo_foto (FileStorage): Objeto do arquivo enviado no request.
        modulo_slug (str): Slug do módulo dono da mídia.
        nome_slot (str): Nome do slot de exibição.

    Returns:
        str: O caminho relativo do arquivo para armazenamento seguro no banco.
    =============================================================================
    """
    pasta_destino = _garantir_diretorio_slot(modulo_slug, nome_slot)

    # Gera um nome único baseado em timestamp e caracteres aleatórios
    timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    rand_sufixo = random.randint(1000, 9999)
    nome_arquivo = f"publ_{timestamp_str}_{rand_sufixo}.webp"

    caminho_completo = os.path.join(pasta_destino, nome_arquivo)

    # Abre a imagem via Pillow, converte para RGB e salva em formato WebP otimizado
    imagem = Image.open(arquivo_foto)
    if imagem.mode in ("RGBA", "P"):
        imagem = imagem.convert("RGB")

    imagem.save(caminho_completo, 'WEBP', quality=85)

    # Retorna o caminho relativo limpo a partir da raiz de uploads do módulo
    caminho_relativo = os.path.join('publicidade', modulo_slug, 'slots', nome_slot, nome_arquivo)
    return caminho_relativo.replace("\\", "/")


@publicidade_bp.route('/admin/configurar/<string:modulo_slug>/<string:nome_slot>/<int:taxonomia_id>', methods=['POST'])
@login_required
@apenas_admin
def admin_configurar_campanha(modulo_slug, nome_slot, taxonomia_id):
    """
    =============================================================================
    ROTA ADMINISTRATIVA: CONFIGURAÇÃO E CRIAÇÃO DE ANÚNCIOS
    =============================================================================
    Processa o envio de formulários administrativos para cadastrar ou atualizar
    uma campanha publicitária atrelada a uma taxonomia, módulo e slot específicos.
    =============================================================================
    """
    taxonomia = Taxonomia.query.get_or_404(taxonomia_id)

    local_id = request.form.get('local_id')
    if not local_id:
        flash('Por favor, selecione uma empresa proprietária do anúncio.', 'danger')
        return redirect(request.referrer or url_for('admin.index'))

    local = Local.query.get_or_404(local_id)
    plano = request.form.get('plano_marketing', 'gratuito')
    arquivo_foto = request.files.get('flyer_arte')

    nome_imagem = None
    if arquivo_foto and arquivo_foto.filename != '':
        try:
            nome_imagem = _salvar_imagem_publ(arquivo_foto, modulo_slug, nome_slot)
        except Exception as e:
            flash(f'Erro ao processar a imagem do anúncio: {str(e)}', 'danger')
            return redirect(request.referrer or url_for('admin.index'))

    url_destino = request.form.get('url_destino')
    if not url_destino:
        flash('Por favor, insira o link de destino (WhatsApp, Instagram ou Site) para o anúncio.', 'danger')
        return redirect(request.referrer or url_for('admin.index'))

    # Tratamento da data de expiração
    data_exp_form = request.form.get('data_expiracao')
    data_expiracao = None
    if data_exp_form:
        try:
            data_expiracao = datetime.strptime(data_exp_form, '%Y-%m-%d')
        except ValueError:
            data_expiracao = None

    # Criação do registro utilizando o modelo padronizado publ_campanha
    nova_campanha = PublCampanha(
        local_id=local.id,
        taxonomia_id=taxonomia_id,
        modulo_slug=modulo_slug,
        nome_slot=nome_slot,
        url_flyer=nome_imagem,
        url_destino=url_destino,
        plano_marketing=plano,
        status='ativo',
        data_expiracao=data_expiracao
    )

    database.session.add(nova_campanha)

    try:
        database.session.commit()
        flash(f'Anúncio para "{local.nome}" configurado com sucesso no slot "{nome_slot}"!', 'success')
    except Exception as e:
        database.session.rollback()
        flash(f'Erro ao salvar anúncio no banco de dados: {str(e)}', 'danger')

    return redirect(request.referrer or url_for('admin.index'))


@publicidade_bp.route('/clique/<int:campanha_id>')
@login_required
def registrar_clique_publ(campanha_id):
    """
    =============================================================================
    ROTA DE RASTREAMENTO DE CLIQUES E BI
    =============================================================================
    Registra a interação do usuário na tabela `publ_clique` para fins de Business
    Intelligence e redireciona o cliente para o link externo de destino cadastrado.
    =============================================================================
    """
    campanha = PublCampanha.query.get_or_404(campanha_id)

    origem = request.args.get('origem', 'banner')
    tag_id = request.args.get('tag_id', campanha.taxonomia_id)

    novo_clique = PublClique(
        anuncio_id=campanha.id,
        usuario_id=current_user.id,
        taxonomia_id=tag_id,
        origem_clique=origem
    )

    database.session.add(novo_clique)

    try:
        database.session.commit()
    except Exception as e:
        database.session.rollback()
        print(f"Erro ao computar clique de BI publicitário: {str(e)}")

    if campanha.url_destino:
        return redirect(campanha.url_destino)

    return redirect(url_for('ver_perfil', usuario_id=campanha.local_id))